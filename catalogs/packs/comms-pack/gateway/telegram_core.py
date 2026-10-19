"""telegram — il connettore Telegram di comms-pack: tutta la logica, zero MCP.

**Dal comms-pack 2.1.0 gira DENTRO il gateway**, come componente
(clodia-platform#522/#558, `telegram_component.py`): non più un backend MCP
stdio. Cambiano solo due sorgenti, e le imposta `register(host)`:

- il token: dal vault del gateway attraverso `host.credentials(
  "telegram_bot_token")` (`TOKEN_SOURCE`), raggiungibile solo finché
  l'interruttore è `connectors: {telegram: pack}`;
- lo stato: il file privato del componente (`STATE_FILE`, sul volume del
  gateway, non leggibile dall'agent-server).

Senza `register` (le suite che caricano questo file da sole) valgono ancora
`TELEGRAM_BOT_TOKEN` e `CLODIA_TELEGRAM_STATE`. Il resto qui sotto è il testo
della versione backend, invariato.

clodia-platform#522, metà «implementazione»: fin qui il pack DICHIARAVA il
connettore (`pack.yaml`, fase 1) mentre a parlare con `api.telegram.org` era il
gateway (`server/tools/telegram.py`). Questo file è quel codice, portato dentro
il pack che lo dichiara. Il gateway lo monta come backend `telegram` e i verbi
tornano agli agenti con lo stesso nome — `telegram.send`, `telegram.inbox`, … —
attraverso `proxy.call_proxied`, quindi dopo la whitelist, i deny, il gate,
l'egress e il taint: **niente di ciò che controllava prima smette di controllare**.

Modello invariato (deciso 29 giu 2026):
- **outbound**: si scrive solo a chat che hanno già scritto al bot (niente
  rubrica statica), e non serve il lease — l'invio passa dal gateway (`channel.deliver`);
- **inbound con lease per-chat**: un agente prende il lease esclusivo su una
  chat per N minuti e finché è valido è l'unico a consumarne i messaggi;
- **niente daemon**: l'inbound è lazy, `getUpdates` con offset persistito a ogni
  `inbox`/`poll`/`send`, serializzata da un lock di processo.

Quattro cose cambiano rispetto all'originale, e sono tutte conseguenze dello
stare fuori dal gateway:

1. **Il token** arriva dall'ambiente (`TELEGRAM_BOT_TOKEN`), che il mount
   riempie con `${VAULT:telegram_bot_token}`: il vault resta del gateway, il
   segreto non passa dal modello né dal `pack.yaml`, e questo processo non ha
   modo di leggere nessun'altra credenziale.
2. **L'identità, la stanza e la chat legata** arrivano come ARGOMENTI che il
   gateway riempie: `agent` e `channel` sovrascrivendo sempre (`context:` del
   manifest), `chat_id` solo se omesso (`defaults:`), e i byte di un file di
   topic al posto del suo percorso (`files:`). Qui non si legge nessun claim
   perché qui il claim non c'è, e non si legge nessuno stato del gateway — né
   il registro dei binding né il filesystem dei topic: fidarsi di questi
   argomenti è legittimo **solo** perché chi li scrive non è il chiamante. Un
   valore vuoto è «non lo so», mai «fai pure».
3. **Nessun controllo di grant**: `tool_allowed()` non si porta appresso. Il PDP
   sta a monte e una seconda copia qui direbbe un'altra cosa il giorno in cui
   una delle due cambia.
4. **Lo stato è del pack** (`CLODIA_TELEGRAM_STATE`, di default dentro la
   cartella del plugin): code per-chat, lease, offset, `bot_username`.

**Stdlib pura**: nessuna dipendenza, nemmeno `mcp` — l'involucro del protocollo
è `telegram_mcp.py`, che importa questo modulo. Nessun import dal gateway: un
pack che importasse `server.*` non sarebbe un pack, sarebbe il gateway per un
altro percorso. E nessuna libreria qui significa che il connettore si verifica
senza avere la stessa versione di `mcp` dell'immagine che lo esegue.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

__version__ = "2.0.0"

#: Set by `telegram_component.register(host)` (see the module docstring).
TOKEN_SOURCE: Optional[Callable[[], str]] = None
STATE_FILE: Optional[str] = None
#: The bot's @username, stored with the token when the owner connected it: a
#: reply to the bot counts as a mention, and the relay recognises a
#: `/start@bot <token>` join (#549). Unset → the one in the state, if any.
BOT_USERNAME_SOURCE: Optional[Callable[[], str]] = None

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_DEFAULT_LEASE_MIN = 10
_MAX_LEASE_MIN = 120


class _StateLock:
    """Serializza il read-modify-write dello stato FRA PROCESSI.

    Il backend NON è un processo solo: il proxy del gateway avvia un processo
    stdio per ogni chiamata, quindi un lock di processo lasciava passare due
    `lease_acquire` concorrenti — entrambi «esclusivi» — e due scritture sullo
    stesso file temporaneo (review di clodia-logic#527). Un `flock` su un file
    accanto allo stato, più un RLock per i thread dello stesso processo;
    rientrante, perché i verbi si richiamano a vicenda tenendolo.
    """

    def __init__(self) -> None:
        self._rlock = threading.RLock()
        self._local = threading.local()

    def _path(self) -> Path:
        return _state_path().with_name(_state_path().name + ".lock")

    def __enter__(self):
        self._rlock.acquire()
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            import fcntl
            p = self._path()
            p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except BaseException:
                os.close(fd)
                self._rlock.release()
                raise
            self._local.fd = fd
        self._local.depth = depth + 1
        return self

    def __exit__(self, *exc) -> None:
        self._local.depth -= 1
        if self._local.depth == 0:
            import fcntl
            fd = self._local.fd
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        self._rlock.release()


# Serializza read-modify-write dello stato (vedi `_StateLock`).
_LOCK = _StateLock()


def _poll_lock(blocking: bool):
    """Il diritto di chiamare `getUpdates`: Telegram ne ammette UNO per bot, e
    due in parallelo si rubano gli update via offset o ricevono 409. Il
    long-poll del relay lo tiene per tutta l'attesa; il refresh lazy di
    `inbox`/`poll` lo prova senza aspettare e, se è occupato, non chiama
    Telegram (i messaggi li sta già raccogliendo chi tiene il lock).
    Ritorna il fd da rilasciare, o None se occupato."""
    import fcntl
    p = _state_path().with_name(_state_path().name + ".poll.lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _poll_unlock(fd) -> None:
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

# ── stato, token, API ────────────────────────────────────────────────────────

def _state_path() -> Path:
    if STATE_FILE:
        return Path(STATE_FILE)
    return Path(os.environ.get("CLODIA_TELEGRAM_STATE")
                or Path(__file__).resolve().parent.parent / "state"
                / "telegram-state.json")


def _load_state() -> dict:
    p = _state_path()
    if not p.is_file():
        return {"offset": 0, "bot_username": None, "last_error": None, "chats": {}}
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"offset": 0, "bot_username": None, "last_error": None, "chats": {}}
    st.setdefault("offset", 0)
    st.setdefault("chats", {})
    st.setdefault("bot_username", None)
    st.setdefault("last_error", None)
    return st


def _save_state(st: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Nome UNICO per processo e thread: con un nome fisso due scritture
    # concorrenti si rubavano il file e la seconda `replace` falliva
    # (FileNotFoundError, review di clodia-logic#527).
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _lease_active(chat: dict) -> Optional[dict]:
    """Il lease se valido (non scaduto), altrimenti None."""
    lease = chat.get("lease")
    if not lease:
        return None
    try:
        exp = datetime.fromisoformat(lease["expiry"])
    except (KeyError, ValueError):
        return None
    return lease if exp > _now() else None


def _raw_token() -> str:
    """The token or "" — from the gateway's vault in a component, from the
    environment otherwise. Never raises on «not configured»."""
    if TOKEN_SOURCE is not None:
        try:
            return str(TOKEN_SOURCE() or "").strip()
        except Exception:  # noqa: BLE001 — no access (switch back, no entry): none
            return ""
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()


def _token() -> str:
    """Il token del bot dall'ambiente del mount.

    Mai dagli argomenti: un token che passasse dal modello sarebbe un token
    nel contesto, e chi può chiamare un verbo non per questo può spedire con
    un altro bot.
    """
    tok = _raw_token()
    if not tok:
        raise RuntimeError(
            "telegram: nessun bot configurato per questa istanza "
            "(credenziale 'telegram_bot_token' nel vault del gateway)")
    return tok


def api_call(token: str, method: str, params: Optional[dict] = None,
             timeout: int = 15) -> dict:
    """Chiamata all'API Bot di Telegram: ritorna `result`, solleva su errore."""
    url = _API_BASE.format(token=token, method=method)
    data = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read().decode("utf-8")).get("description", "")
        except Exception:  # noqa: BLE001
            desc = e.reason
        raise RuntimeError(f"telegram {method} HTTP {e.code}: {desc}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"telegram {method} rete: {e.reason}") from None
    if not payload.get("ok"):
        raise RuntimeError(f"telegram {method}: {payload.get('description', 'errore')}")
    return payload.get("result")


# ── contesto che mette il gateway ────────────────────────────────────────────

def _agent(a: dict) -> str:
    """Chi sta chiamando, secondo il GATEWAY (`context: {…: agent}`).

    Vuoto = chiamata senza agente: il lease non ha un titolare e si rifiuta,
    invece di attribuirlo a una stringa vuota che poi nessuno può rilasciare.
    """
    return str(a.get("agent") or "").strip()


def _holder(a: dict) -> str:
    me = _agent(a)
    if not me:
        raise ValueError(
            "lease senza agente: questa chiamata non porta l'identità che il "
            "gateway dichiara nel manifest (context: agent)")
    return me


def _channel(a: dict) -> str:
    """La stanza da cui si chiama, secondo il gateway (`context: {…: channel}`)."""
    return str(a.get("channel") or "").strip()


def _resolve_chat(chat: str) -> str:
    """Un riferimento chat → chat_id. Accetta l'id numerico oppure il NOME del
    gruppo (match esatto sul titolo, poi parziale) fra le chat note."""
    c = str(chat).strip()
    if c.lstrip("-").isdigit():
        return c
    low = c.lower()
    with _LOCK:
        chats = _load_state().get("chats", {})
    for cid, meta in chats.items():
        if str(meta.get("title", "")).lower() == low:
            return cid
    for cid, meta in chats.items():
        if low and low in str(meta.get("title", "")).lower():
            return cid
    raise ValueError(f"chat '{chat}' non trovata (né chat_id numerico né titolo noto)")


def _resolve_chat_or_current(a: dict) -> str:
    """La chat di questa chiamata.

    `chat_id` esplicito → `_resolve_chat`. Omesso → **lo riempie il gateway**
    con la chat legata al canale chiamante (blocco `defaults:` del manifest):
    il registro dei binding è suo, tiene quelli di tutti i connettori e questo
    processo non lo legge.

    Che l'omissione sia la strada preferita viene dal 23 set 2026: due gruppi
    con nomi quasi identici — «Davide & Clodia» e «Davide & Clodia Colony
    Blogging» — hanno prodotto invii ripetuti al gruppo SBAGLIATO perché chi
    chiamava indovinava un nome invece di usare il binding del topic corrente,
    l'unica fonte che non può sbagliare per costruzione.

    Arrivare qui senza chat significa che la chiamata non è passata dal
    gateway: si rifiuta, invece di indovinare.
    """
    chat = a.get("chat_id")
    if chat:
        return _resolve_chat(chat)
    raise ValueError(
        "nessuna chat per questa chiamata: il gateway riempie 'chat_id' con la "
        "chat legata al topic chiamante, oppure passane uno esplicito")


def thread_id_of(value) -> int | None:
    """Il thread di un forum (`message_thread_id`), o None (clodia-platform#548).

    Decide solo DOVE compare il messaggio dentro una chat già autorizzata:
    l'egress resta controllato sulla chat (`tg:<chat_id>`).
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("'thread_id' deve essere un intero positivo")
    try:
        tid = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("'thread_id' deve essere un intero positivo") from None
    if tid <= 0:
        raise ValueError("'thread_id' deve essere un intero positivo")
    return tid


def _with_thread(params: dict, thread_id) -> dict:
    tid = thread_id_of(thread_id)
    if tid is not None:
        params["message_thread_id"] = tid
    return params


# ── inbound ──────────────────────────────────────────────────────────────────

def _chat_title(chat: dict) -> str:
    if chat.get("title"):
        return chat["title"]
    name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
    if chat.get("username"):
        name = f"{name} (@{chat['username']})".strip()
    return name or str(chat.get("id"))


def _extract_file(msg: dict):
    """L'allegato del messaggio (documento/foto/video/audio/voce), o None."""
    if msg.get("document"):
        d = msg["document"]
        return {"file_id": d["file_id"],
                "file_name": d.get("file_name") or f"document_{str(d['file_id'])[:10]}",
                "mime": d.get("mime_type")}
    if msg.get("photo"):
        p = msg["photo"][-1]   # la risoluzione più alta
        return {"file_id": p["file_id"],
                "file_name": f"photo_{str(p['file_id'])[:10]}.jpg", "mime": "image/jpeg"}
    for key, ext in (("video", "mp4"), ("audio", "mp3"), ("voice", "ogg"),
                     ("animation", "mp4")):
        if msg.get(key):
            m = msg[key]
            return {"file_id": m["file_id"],
                    "file_name": m.get("file_name") or f"{key}_{str(m['file_id'])[:10]}.{ext}",
                    "mime": m.get("mime_type")}
    return None


def _message_row(upd: dict, msg: dict) -> dict:
    frm = msg.get("from") or {}
    text = msg.get("text") or msg.get("caption") or ""
    return {
        "update_id": upd.get("update_id"),
        "message_id": msg.get("message_id"),
        # `from` è la stringa di display; `from_username` è l'handle stabile con
        # cui il relay mappa il mittente a un principal registrato.
        "from": " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
                or (frm.get("username") or str(frm.get("id"))),
        "from_id": frm.get("id"),
        "from_username": frm.get("username"),
        "text": text,
        "date": msg.get("date"),
        "thread_id": (msg.get("message_thread_id")
                      if msg.get("is_topic_message") else None),
    }


def _refresh(st: dict, token: str) -> int:
    """getUpdates lazy: accoda i messaggi nuovi nelle code per-chat e avanza
    l'offset. Best-effort: un errore di rete finisce in `last_error` e non rompe
    l'operazione in corso — le code già accumulate restano leggibili."""
    fd = _poll_lock(blocking=False)
    if fd is None:
        return 0          # un altro consumer sta già leggendo gli update
    try:
        updates = api_call(token, "getUpdates",
                           {"offset": st["offset"], "timeout": 0, "limit": 100})
    except RuntimeError as e:
        st["last_error"] = str(e)[:300]
        return 0
    finally:
        _poll_unlock(fd)
    st["last_error"] = None
    n = 0
    for upd in updates or []:
        st["offset"] = max(st["offset"], int(upd["update_id"]) + 1)
        msg = upd.get("message") or upd.get("edited_message")
        if not msg or "chat" not in msg:
            continue
        chat = msg["chat"]
        cid = str(chat["id"])
        slot = st["chats"].setdefault(cid, {"title": "", "queue": [], "lease": None,
                                            "last_preview": "", "type": chat.get("type")})
        slot["title"] = _chat_title(chat)
        slot["type"] = chat.get("type")
        entry = _message_row(upd, msg)
        slot["queue"].append(entry)
        text = entry["text"]
        slot["last_preview"] = (text[:80] + "…") if len(text) > 80 else text
        n += 1
    return n


# ── i tool ───────────────────────────────────────────────────────────────────

def inbox(a: dict) -> dict:
    """Chat con messaggi in arrivo (metadati, non consuma)."""
    me = _agent(a)
    with _LOCK:
        st = _load_state()
        _refresh(st, _token())
        out = []
        for cid, c in st["chats"].items():
            lease = _lease_active(c)
            out.append({
                "chat_id": cid,
                "title": c.get("title"),
                "type": c.get("type"),
                "pending": len(c.get("queue") or []),
                "last_preview": c.get("last_preview"),
                "leased_by": lease["holder"] if lease else None,
                "lease_expiry": lease["expiry"] if lease else None,
                "mine": bool(lease and me and lease["holder"] == me),
            })
        _save_state(st)
    out.sort(key=lambda x: x["pending"], reverse=True)
    return {"chats": out, "bot_username": st.get("bot_username"),
            "last_error": st.get("last_error")}


def lease_acquire(a: dict) -> dict:
    """Lease esclusivo su una chat per N minuti."""
    cid = str(a["chat_id"])
    minutes = max(1, min(int(a.get("minutes") or _DEFAULT_LEASE_MIN), _MAX_LEASE_MIN))
    me = _holder(a)
    with _LOCK:
        st = _load_state()
        _refresh(st, _token())
        chat = st["chats"].get(cid)
        if chat is None:
            _save_state(st)
            raise ValueError(
                f"chat '{cid}' sconosciuta: un lease si può prendere solo su una "
                f"chat da cui è arrivato almeno un messaggio (vedi telegram.inbox)")
        lease = _lease_active(chat)
        if lease and lease["holder"] != me:
            _save_state(st)
            raise PermissionError(
                f"lease su chat '{cid}' tenuto da '{lease['holder']}' fino a "
                f"{lease['expiry']}")
        expiry = _now() + timedelta(minutes=minutes)
        chat["lease"] = {"holder": me, "expiry": _iso(expiry)}
        _save_state(st)
    return {"chat_id": cid, "holder": me, "expiry": _iso(expiry),
            "minutes": minutes, "pending": len(chat.get("queue") or [])}


def poll(a: dict) -> dict:
    """Consuma (e svuota) la coda di una chat. Richiede il lease del chiamante."""
    cid = str(a["chat_id"])
    me = _holder(a)
    with _LOCK:
        st = _load_state()
        _refresh(st, _token())
        chat = st["chats"].get(cid)
        if chat is None:
            _save_state(st)
            raise ValueError(f"chat '{cid}' sconosciuta (nessun messaggio ricevuto)")
        lease = _lease_active(chat)
        if not lease or lease["holder"] != me:
            _save_state(st)
            raise PermissionError(
                f"per consumare i messaggi di '{cid}' devi detenere il lease "
                f"(telegram.lease_acquire); attuale: "
                f"{lease['holder'] if lease else 'nessuno'}")
        msgs = chat.get("queue") or []
        chat["queue"] = []
        _save_state(st)
    return {"chat_id": cid, "messages": msgs, "count": len(msgs),
            "lease_expiry": lease["expiry"]}


def lease_release(a: dict) -> dict:
    """Rilascia il lease su una chat (no-op se non lo detieni)."""
    cid = str(a["chat_id"])
    me = _holder(a)
    released = False
    with _LOCK:
        st = _load_state()
        chat = st["chats"].get(cid)
        lease = _lease_active(chat) if chat else None
        if lease and lease["holder"] == me:
            chat["lease"] = None
            released = True
        _save_state(st)
    return {"chat_id": cid, "released": released}


def send(a: dict) -> dict:
    """Invia un messaggio a una chat. LEASE-FREE: l'invio passa dal
    gateway (`channel.deliver`), un lease esclusivo qui sarebbe solo attrito."""
    text = str(a.get("text") or "")
    if not text.strip():
        raise ValueError("'text' non può essere vuoto")
    cid = _resolve_chat_or_current(a)
    with _LOCK:
        token = _token()
    res = api_call(token, "sendMessage",
                   _with_thread({"chat_id": int(cid), "text": text},
                                a.get("thread_id")))
    return {"ok": True, "chat_id": cid, "message_id": res.get("message_id")}


def _post_multipart(token: str, method: str, fields: dict, file_field: str,
                    filename: str, filedata: bytes, mime: str) -> dict:
    """POST multipart/form-data all'API (sendDocument/sendPhoto), stdlib pura."""
    boundary = "----clodia" + os.urandom(16).hex()
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f"name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                  f"name=\"{file_field}\"; filename=\"{filename}\"\r\n"
                  f"Content-Type: {mime}\r\n\r\n").encode())
    parts.append(filedata)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    url = _API_BASE.format(token=token, method=method)
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"telegram {method} HTTP {e.code}: {detail}") from e
    if not payload.get("ok"):
        raise RuntimeError(f"telegram {method}: {payload.get('description')}")
    return payload["result"]


def send_file(a: dict) -> dict:
    """Invia un file come allegato (sendDocument) o come foto (sendPhoto).

    I BYTE li passa il gateway (`content_b64`), che è l'unico a poter leggere un
    file di topic: lì stanno il compartimento (chi chiama dev'essere
    partecipante) e l'audit di ciò che attraversa il confine. Un pack che
    leggesse da sé il filesystem dei topic aggirerebbe entrambi.
    """
    import base64
    import mimetypes
    filename = str(a.get("filename") or "")
    data = base64.b64decode(str(a.get("content_b64") or ""))
    if not filename or not data:
        # I due argomenti li riempie il gateway leggendo il file che `path`
        # nomina (blocco `files:` del manifest). Vuoti = la consegna non è
        # avvenuta: si rifiuta invece di spedire un allegato vuoto.
        raise ValueError(
            "nessun file consegnato dal gateway per questa chiamata: indica "
            "'path' (il file dentro il topic), non il contenuto")
    cid = _resolve_chat_or_current(a)
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    as_photo = mime.startswith("image/") and \
        not filename.lower().endswith((".svg", ".gif"))
    method, field = ("sendPhoto", "photo") if as_photo else ("sendDocument", "document")
    fields = {"chat_id": str(cid)}
    if a.get("caption"):
        fields["caption"] = str(a["caption"])
    _with_thread(fields, a.get("thread_id"))
    with _LOCK:
        token = _token()
    res = _post_multipart(token, method, fields, field, filename, data, mime)
    return {"ok": True, "chat_id": cid, "message_id": res.get("message_id"),
            "as_photo": as_photo}


def roster(a: dict) -> dict:
    """Amministratori noti di un gruppo, con uid e username."""
    got = api_call(_token(), "getChatAdministrators",
                   {"chat_id": str(a["chat_id"])}) or []
    out = []
    for m in got:
        u = m.get("user") or {}
        if u.get("is_bot"):
            continue
        out.append({"uid": str(u.get("id")), "username": u.get("username"),
                    "name": " ".join(x for x in (u.get("first_name"),
                                                 u.get("last_name")) if x),
                    "status": m.get("status")})
    # Telegram non espone a un bot l'elenco COMPLETO dei membri: si ottengono
    # gli amministratori. Lo si dice, invece di far credere che la lista sia
    # tutta — chi mappa le persone deve sapere che gli altri vanno aggiunti a
    # mano dal loro uid.
    return {"members": out, "complete": False,
            "note": ("Telegram espone a un bot i soli amministratori. Gli altri "
                     "membri vanno mappati col loro uid, che compare quando "
                     "scrivono nel gruppo.")}


def status(a: dict) -> dict:
    """Stato non sensibile per la card di setup: configurato?, @username del
    bot, chat note/attive, ultimo errore. Mai il token."""
    st = _load_state()
    active = sum(1 for c in st["chats"].values()
                 if (c.get("queue") or []) or _lease_active(c))
    return {
        "configured": bool(_raw_token()),
        "bot_username": st.get("bot_username"),
        "known_chats": len(st["chats"]),
        "active_chats": active,
        "last_error": st.get("last_error"),
    }


def updates(a: dict) -> dict:
    """LONG-POLL: `getUpdates` con timeout lungo, per il RELAY del gateway.

    Telegram ammette un solo consumer `getUpdates` per bot: una volta che
    l'implementazione è qui, questa è l'unica porta da cui gli update possono
    entrare — relay compreso. Per questo il verbo esiste, e per questo non è un
    verbo da agente: chi lo chiamasse svuoterebbe la coda che il relay deve
    leggere. Il gateway lo tiene fuori dalla portata degli agenti (seed
    nessun agente lo ha) e lo usa per il proprio percorso interno.
    """
    timeout = max(0, min(int(a.get("timeout") or 25), 60))
    # Il diritto a `getUpdates` per tutta l'attesa (vedi `_poll_lock`), ma
    # FUORI dal lock dello stato: congelare `send` per `timeout` secondi
    # sarebbe un difetto, non una precauzione.
    fd = _poll_lock(blocking=True)
    try:
        with _LOCK:
            offset = _load_state()["offset"]
            token = _token()
        got = api_call(token, "getUpdates",
                       {"offset": offset, "timeout": timeout, "limit": 100},
                       timeout=timeout + 10)
        return _absorb_updates(got)
    finally:
        _poll_unlock(fd)


def _absorb_updates(got) -> dict:
    out = []
    with _LOCK:
        st = _load_state()
        bot_uname = st.get("bot_username")
        if not bot_uname and BOT_USERNAME_SOURCE is not None:
            try:
                bot_uname = BOT_USERNAME_SOURCE() or None
            except Exception:  # noqa: BLE001 — unknown: the join check stays lenient
                bot_uname = None
            if bot_uname:
                st["bot_username"] = bot_uname
        for upd in got or []:
            st["offset"] = max(st["offset"], int(upd["update_id"]) + 1)
            msg = upd.get("message") or upd.get("edited_message")
            if not msg or "chat" not in msg:
                continue
            rfrm = (msg.get("reply_to_message") or {}).get("from") or {}
            row = _message_row(upd, msg)
            row.update({
                "chat_id": str(msg["chat"]["id"]),
                "chat_title": _chat_title(msg["chat"]),
                # Il TIPO della chat serve al gateway per riconoscere un join
                # per link (`claim` accetta solo i gruppi): senza, un join in un
                # gruppo verrebbe scartato e finirebbe nel topic come messaggio.
                "chat_type": msg["chat"].get("type"),
                "file": _extract_file(msg),
                # Rispondere a un messaggio del BOT conta come menzione diretta.
                "reply_to_bot": bool(rfrm.get("is_bot") and bot_uname
                                     and rfrm.get("username") == bot_uname),
            })
            out.append(row)
        st["last_error"] = None
        _save_state(st)
    # `bot_username` viaggia con la risposta: al gateway serve per riconoscere
    # un `/start@bot <token>` e non vale una seconda chiamata a ogni poll.
    return {"messages": out, "count": len(out), "bot_username": bot_uname}


def download(a: dict) -> dict:
    """Scarica un allegato (getFile + download): `{content_b64, size}`.

    Serve al relay per depositare nel topic il file che qualcuno ha mandato in
    chat; i byte li scrive il gateway, che è l'unico a sapere se quel topic può
    riceverli e a marcarne la provenienza.
    """
    import base64
    token = _token()
    info = api_call(token, "getFile", {"file_id": str(a["file_id"])})
    fpath = (info or {}).get("file_path")
    if not fpath:
        raise RuntimeError("getFile senza file_path")
    url = f"https://api.telegram.org/file/bot{token}/{fpath}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = resp.read()
    return {"content_b64": base64.b64encode(data).decode("ascii"), "size": len(data)}


# ── la superficie: nomi, schemi, dispatch ────────────────────────────────────
#
# Descritta con dict e non con `mcp.types.Tool`: questo modulo è il connettore e
# non deve dipendere da nessuna libreria oltre la stdlib. L'involucro MCP
# (`telegram_mcp.py`) traduce ogni voce in un `Tool`, e in cambio tutta la
# logica — e la sua tabella dei verbi — resta verificabile da una suite che non
# ha (e non deve avere) la stessa versione di `mcp` del gateway.

_CONTEXT_PROPS = {
    "channel": {"type": "string",
                "description": "riempito dal gateway: il topic da cui si chiama"},
    "agent": {"type": "string",
              "description": "riempito dal gateway: l'agente che chiama"},
}


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": {**props, **_CONTEXT_PROPS},
            "required": list(required or [])}


TOOL_SPECS: list[dict] = [
    dict(name="inbox",
         description=("Chat Telegram con messaggi in arrivo (metadati, NON consuma): "
                      "per ognuna chat_id, titolo, n. messaggi pendenti, anteprima e chi "
                      "detiene il lease. Punto di partenza prima di prendere un lease."),
         inputSchema=_schema({})),
    dict(name="lease_acquire",
         description=("Acquisisce il lease ESCLUSIVO su una chat per N minuti: finché è "
                      "valido sei l'unico a consumarne i messaggi e a poterle scrivere. "
                      "Fallisce se un altro agente la detiene. Solo chat che hanno scritto."),
         inputSchema=_schema({
             "chat_id": {"type": "string", "description": "ID chat (da telegram.inbox)"},
             "minutes": {"type": "integer",
                         "description": "Durata lease (default 10, max 120)"}},
             ["chat_id"])),
    dict(name="poll",
         description=("Consuma (svuota) i messaggi in coda di una chat. Richiede un lease "
                      "attivo del chiamante su quella chat."),
         inputSchema=_schema({"chat_id": {"type": "string"}}, ["chat_id"])),
    dict(name="send",
         description=("Invia un messaggio a una chat/gruppo (lease-free: sei l'unico "
                      "mittente). Vincolo Telegram: la chat deve aver già contattato il "
                      "bot, o il bot dev'essere membro del gruppo. `chat_id` accetta anche "
                      "il NOME del gruppo. `chat_id` OMESSO → la chat collegata al topic "
                      "da cui stai chiamando: PREFERISCI omettere, non indovinare/ricordare "
                      "un nome — due gruppi con nomi quasi identici hanno già causato "
                      "invii al gruppo sbagliato."),
         inputSchema=_schema({
             "chat_id": {"type": "string",
                         "description": "opzionale: default la chat del topic corrente"},
             "text": {"type": "string"},
             "thread_id": {"type": "integer",
                           "description": "opzionale: il thread di un gruppo-forum; "
                                          "omesso = thread Generale"}},
             ["text"])),
    dict(name="send_file",
         description=("Invia un FILE del topic a una chat/gruppo come allegato (o come "
                      "foto se è un'immagine). Passa `path` (il file dentro il topic, es. "
                      "`local/foo.png`): i byte li legge il GATEWAY, che è l'unico a poter "
                      "leggere un file di topic. `tier`/`name` solo se il file è in un "
                      "topic diverso da quello del gruppo. `chat_id` OMESSO → la chat "
                      "collegata al topic da cui stai chiamando."),
         inputSchema=_schema({
             "chat_id": {"type": "string",
                         "description": "opzionale: default la chat del topic corrente"},
             "path": {"type": "string",
                      "description": "path del file nel topic, es. local/foo.png"},
             "tier": {"type": "string", "description": "opzionale (override topic)"},
             "name": {"type": "string", "description": "opzionale (override topic)"},
             "caption": {"type": "string"},
             "thread_id": {"type": "integer"},
             "filename": {"type": "string",
                          "description": "riempito dal gateway: il nome del file"},
             "content_b64": {"type": "string",
                             "description": "riempito dal gateway: i byte del file"}},
             ["path"])),
    dict(name="lease_release",
         description="Rilascia anticipatamente il lease su una chat (no-op se non lo detieni).",
         inputSchema=_schema({"chat_id": {"type": "string"}}, ["chat_id"])),
    dict(name="roster",
         description=("Amministratori e membri noti di un gruppo, con uid e username: "
                      "serve a mappare le persone senza digitare numeri."),
         inputSchema=_schema({"chat_id": {"type": "string"}}, ["chat_id"])),
    dict(name="status",
         description=("Stato del connettore: bot configurato, @username, chat note e "
                      "attive, ultimo errore. Mai il token."),
         inputSchema=_schema({})),
    dict(name="updates",
         description=("USO INTERNO (relay del gateway): long-poll getUpdates di TUTTE le "
                      "chat. Consuma la coda condivisa — non è un verbo da agente."),
         inputSchema=_schema({"timeout": {"type": "integer"}})),
    dict(name="download",
         description=("USO INTERNO (relay del gateway): scarica un allegato Telegram e "
                      "ne ritorna i byte, che il gateway deposita nel topic."),
         inputSchema=_schema({"file_id": {"type": "string"}}, ["file_id"])),
]

DISPATCH = {
    "inbox": inbox,
    "lease_acquire": lease_acquire,
    "poll": poll,
    "send": send,
    "send_file": send_file,
    "lease_release": lease_release,
    "roster": roster,
    "status": status,
    "updates": updates,
    "download": download,
}
