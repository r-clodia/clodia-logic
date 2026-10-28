"""The deterministic mail courier (clodia-platform#531).

Moves inbound mail into the topics whose room has a mailbox in its OWN
`inbox:` list — code, never a model, and never the message body:

- asks the gateway which rooms have a mailbox (`/internal/mail/courier/rooms`);
- for each, the envelopes it has not delivered yet
  (`/internal/mail/courier/envelopes`): sender, recipients, subject, date,
  attachment names. The gateway runs comms-pack's component inside the room's
  claims, so the room's `inbox:` list and, on a strict room, its declared
  senders (#503) apply before anything leaves the gateway;
- stores each attachment in the topic's files as `untrusted`, byte-identical
  (`/internal/mail/courier/attachment` → `topics_client.put_file`);
- posts the envelope into the topic under the connector's identity (`posta`,
  kind `proxy`), naming the saved files. Agents that work on the topic read
  the message, if they need to, with `email.read` under the usual taint rules.

The first pass over a mailbox only records what is there (no flood of old
mail into the topic): delivery starts with what arrives afterwards.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os

import requests

from ..colony import pki
from ..config import data_path
from . import topics_client

LOG = logging.getLogger("agent-server.mail_courier")

_PRINCIPAL = os.environ.get("CLODIA_PROVIDER_PRINCIPAL", "clodia")
_TOKEN_TTL = 300
_HTTP_TIMEOUT = 60
#: The author of the courier's posts: the connector, not an agent.
AUTHOR = "posta"
#: Keys remembered per mailbox and room.
_KEYS_CAP = 500


def _base_url() -> str:
    explicit = os.environ.get("CLODIA_TOOLS_MAIL_COURIER_URL")
    if explicit:
        return explicit.rstrip("/")
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/")
    base = mcp.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return f"{base}/internal/mail/courier"


def _headers() -> dict[str, str]:
    # The service role: the gateway's courier routes accept the agent-server
    # only, never a spawn whose token also says `clodia` (review of #416).
    tok = pki.mint_session_token(_PRINCIPAL, ttl_seconds=_TOKEN_TTL,
                                 role=pki.ROLE_AGENT_SERVER)
    return {"Authorization": f"Bearer {tok}"}


# ── the gateway ───────────────────────────────────────────────────────────

def rooms() -> list[dict]:
    # `indexed=1`: this courier reads message-box rows through the index
    # (#585), so the gateway may offer one per topic. An older courier does
    # not ask, and gets only the rows it can afford (review of #558).
    r = requests.get(f"{_base_url()}/rooms", headers=_headers(), timeout=_HTTP_TIMEOUT,
                     params={"indexed": "1"})
    r.raise_for_status()
    return list(r.json().get("rooms") or [])


def _tag(room: dict) -> dict:
    """The row's message-box tag (#583), passed back exactly as `rooms()` gave
    it: the gateway accepts only the (room, mailbox, tag) it offered."""
    return {"tag": room["tag"]} if room.get("tag") else {}


def envelopes(room: dict, skip: list[str], keys_only: bool = False,
              only: list[str] | None = None) -> dict:
    body = {"tier": room["tier"], "name": room["name"], "account": room["account"],
            "skip": list(skip), "keys_only": bool(keys_only), **_tag(room)}
    if only is not None:
        body["only"] = list(only)
    r = requests.post(f"{_base_url()}/envelopes", headers=_headers(), timeout=_HTTP_TIMEOUT,
                      json=body)
    r.raise_for_status()
    return r.json()


def box() -> dict | None:
    """The message-box index (#585): `{account, tags: {CODE: [keys]}}` from ONE
    search of the colony mailbox. None when the gateway or comms-pack cannot
    give it (older versions, or the search failed)."""
    try:
        r = requests.post(f"{_base_url()}/box", headers=_headers(), timeout=_HTTP_TIMEOUT,
                          json={})
    except requests.RequestException as e:
        LOG.warning("mail courier: message-box index unavailable (%s)", type(e).__name__)
        return None
    if r.status_code != 200:
        LOG.warning("mail courier: message-box index unavailable (%s)", r.status_code)
        return None
    tags = (r.json() or {}).get("tags") or {}
    return {str(k).upper(): [str(x) for x in (v or [])] for k, v in tags.items()}


def attachment(room: dict, email_id: str, filename: str) -> dict:
    r = requests.post(f"{_base_url()}/attachment", headers=_headers(), timeout=_HTTP_TIMEOUT,
                      json={"tier": room["tier"], "name": room["name"],
                            "account": room["account"], "email_id": str(email_id),
                            "filename": filename, **_tag(room)})
    r.raise_for_status()
    return r.json()


def eml(room: dict, email_id: str, key: str = "") -> dict:
    """The whole message (base64) for the topic's `.eml` copy (#576). `key`:
    the envelope's, so the gateway refuses another message under the same id."""
    r = requests.post(f"{_base_url()}/eml", headers=_headers(), timeout=_HTTP_TIMEOUT,
                      json={"tier": room["tier"], "name": room["name"],
                            "account": room["account"], "email_id": str(email_id),
                            "key": str(key or ""), **_tag(room)})
    r.raise_for_status()
    return r.json()


# ── state ─────────────────────────────────────────────────────────────────

def _state_path(room: dict):
    """One file per room+mailbox; the name is a hash of the three parts, so
    `a_b`+`c` and `a`+`b_c` never share a file (review of #549)."""
    import hashlib
    d = data_path("mail-courier-state")
    d.mkdir(parents=True, exist_ok=True)
    parts = [room["tier"], room["name"], room["account"]]
    if room.get("tag"):
        parts.append("tag:" + str(room["tag"]))   # #583: the message-box row
    key = "\x00".join(parts)
    return d / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"


def _load(room: dict) -> dict:
    p = _state_path(room)
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
        return {"primed": bool(s.get("primed")), "keys": list(s.get("keys") or []),
                "fails": dict(s.get("fails") or {}),
                "senders_sig": str(s.get("senders_sig") or "")}
    except (OSError, ValueError):
        return {"primed": False, "keys": [], "fails": {}, "senders_sig": ""}


def _save(room: dict, state: dict) -> None:
    state["keys"] = list(state.get("keys") or [])[-_KEYS_CAP:]
    state["fails"] = dict(list((state.get("fails") or {}).items())[-50:])
    _state_path(room).write_text(json.dumps(state), encoding="utf-8")


# ── delivery ──────────────────────────────────────────────────────────────

def _safe_filename(name: str) -> str:
    base = os.path.basename(str(name or "allegato")).replace("\\", "_").strip() or "allegato"
    keep = "".join(c if c.isalnum() or c in "._- " else "_" for c in base).strip()
    return (keep or "allegato")[:120]


#: Attempts at one envelope before it is posted without what keeps failing.
MAX_ATTEMPTS = 3


def _plain(value, limit: int = 300) -> str:
    """A sender-chosen header as one inert line: no line breaks (they would
    forge lines of the post), no `@` that mentions an agent of the room."""
    text = " ".join(str(value or "").split())
    return text.replace("@", "＠")[:limit]


#: Where the courier keeps the whole message of each delivery (#576).
EML_DIR = "local/mailbox"


def eml_name(env: dict) -> str:
    """`local/mailbox/<YYYY-MM-DD>-<key8>.eml`: the date of the message when
    its header parses, today's otherwise; the key makes it unique."""
    from datetime import date
    from email.utils import parsedate_to_datetime
    try:
        giorno = parsedate_to_datetime(str(env.get("date") or "")).date().isoformat()
    except Exception:  # noqa: BLE001 — a sender-chosen header never names the file
        giorno = date.today().isoformat()
    return f"{EML_DIR}/{giorno}-{str(env.get('key') or 'x')[:8]}.eml"


def body_name(eml_path: str) -> str:
    """The trusted text of an authenticated mail sits next to its `.eml` (#586)."""
    return eml_path[:-len(".eml")] + ".txt" if eml_path.endswith(".eml") else ""


#: Where someone else's text starts inside the sender's own: a forward or a
#: quoted reply, as the common clients write it. The trusted text stops at the
#: first one (#586; second review of #558).
_re = __import__("re")
_FORWARD_RE = _re.compile(
    r"^\s*-{3,}\s*(forwarded message|original message|messaggio inoltrato|"
    r"messaggio originale)\s*-*\s*$|^\s*(begin forwarded message|inizio messaggio inoltrato)"
    r"\s*:?\s*$|^\s*_{10,}\s*$", _re.I)
_HEADER_FROM_RE = _re.compile(r"^\s*(from|da|de|von)\s*:\s", _re.I)
_HEADER_NEXT_RE = _re.compile(r"^\s*(sent|inviato|date|data|subject|oggetto|to|a)\s*:\s", _re.I)
_WROTE_RE = _re.compile(r"(wrote|ha scritto|a écrit|schrieb)\s*:\s*$", _re.I)
_ON_RE = _re.compile(r"^\s*(on|il|le|am)\b", _re.I)


def _own_lines(text: str) -> str:
    """The sender's own lines: up to the first forward/quote header (a forward
    banner, an Outlook `____` rule, a `From:`+`Sent:` header block, an «On …
    wrote:» line even wrapped on two lines), without quoted (`>`) lines."""
    lines = text.splitlines()
    keep = []
    for i, line in enumerate(lines):
        if _FORWARD_RE.match(line):
            break
        if _HEADER_FROM_RE.match(line) and any(
                _HEADER_NEXT_RE.match(nxt) for nxt in lines[i + 1:i + 5]):
            break
        if _WROTE_RE.search(line) and (_ON_RE.match(line) or (
                i > 0 and _ON_RE.match(lines[i - 1]))):
            if i > 0 and _ON_RE.match(lines[i - 1]) and not _WROTE_RE.search(lines[i - 1]):
                keep = keep[:-1] if keep and keep[-1] == lines[i - 1] else keep
            break
        if line.strip().startswith(">"):
            continue
        keep.append(line)
    return "\n".join(keep).strip()


def _decode(part) -> str:
    payload = part.get_payload(decode=True) or b""
    try:
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    except LookupError:   # an unknown charset name (e.g. unknown-8bit)
        return payload.decode("utf-8", errors="replace")


def _is_body_part(part) -> bool:
    return (part.get_content_type() == "text/plain"
            and not part.get_filename()
            and not part.get_param("name")
            and "attachment" not in str(part.get("Content-Disposition") or "").lower())


def plain_body(raw: bytes) -> str:
    """The sender's own text of a message (#586), or "".

    Only the BODY: a top-level `text/plain`, or the `text/plain` of a
    top-level `multipart/alternative`, or the first part of a top-level
    `multipart/mixed`/`related` when it is one of those. Never a part with a
    file name, never anything under a `message/*` part. Then cut at the first
    forward or quoted-reply header (`_own_lines`): what follows is someone
    else's text and stays in the untrusted `.eml`. An inline forward the
    client did not mark is the residual risk, documented in SETUP."""
    import email
    try:
        msg = email.message_from_bytes(raw)
    except Exception:  # noqa: BLE001
        return ""

    def from_alternative(part) -> str:
        for sub in part.get_payload() or []:
            if _is_body_part(sub):
                return _decode(sub)
        return ""

    ctype = msg.get_content_type()
    text = ""
    if _is_body_part(msg):
        text = _decode(msg)
    elif ctype == "multipart/alternative":
        text = from_alternative(msg)
    elif ctype in ("multipart/mixed", "multipart/related"):
        parts = msg.get_payload() or []
        first = parts[0] if parts else None
        if first is not None and _is_body_part(first):
            text = _decode(first)
        elif first is not None and first.get_content_type() == "multipart/alternative":
            text = from_alternative(first)
    return _own_lines(text)


def auth_line(auth: dict | None) -> str:
    """The envelope's line on the sender's authentication (#586)."""
    if not isinstance(auth, dict):
        return ""
    if auth.get("authenticated"):
        return (f"Mittente autenticato ({str(auth.get('method') or '').upper()}, "
                f"dominio {_plain(auth.get('domain'), 120)})")
    return f"Mittente NON autenticato: {_plain(auth.get('reason'), 160) or 'esito assente'}"


def envelope_text(env: dict, saved: list[str], failed: list[str],
                  eml_path: str = "", auth: dict | None = None,
                  body_path: str = "") -> str:
    """The post: the envelope only, never a body. An authenticated mail's own
    text is a FILE of the topic (#586), named here, not quoted."""
    righe = [f"📧 Nuova mail da {_plain(env.get('from')) or '?'}",
             f"Oggetto: {_plain(env.get('subject')) or '(senza oggetto)'}",
             f"Data: {_plain(env.get('date'), 80) or '?'}"]
    if env.get("to"):
        righe.append(f"A: {_plain(env['to'])}")
    if saved:
        righe.append("Allegati salvati: " + ", ".join(f"`{p}`" for p in saved))
    if failed:
        righe.append("Allegati non salvati: " + ", ".join(_plain(f, 120) for f in failed))
    if auth_line(auth):
        righe.append(auth_line(auth))
    if body_path:
        righe.append(f"Testo (fidato): `{body_path}`")
    if eml_path:
        righe.append(f"Messaggio completo (non fidato): `{eml_path}`")
    righe.append(f"(id {env.get('id')} nella casella)")
    return "\n".join(righe)


async def _deliver(room: dict, env: dict) -> dict:
    """Deliver one envelope; returns what a mail trigger lists (#628)."""
    saved, failed = [], []
    for name in env.get("attachments") or []:
        try:
            got = await asyncio.to_thread(attachment, room, env["id"], name)
            raw = base64.b64decode(got["content_b64"])
            fname = f"posta-{str(env.get('key') or '')[:8]}-{_safe_filename(got.get('filename') or name)}"
            await topics_client.async_put_file(
                room["tier"], room["name"], fname,
                base64.b64encode(raw).decode("ascii"), provenance="untrusted")
            saved.append(f"files/{fname}")
        except Exception as e:  # noqa: BLE001 — one attachment never stops the envelope
            LOG.warning("mail courier: attachment of %s/%s not saved (%s)",
                        room["tier"], room["name"], type(e).__name__)
            failed.append(str(name))
    # The whole message, untrusted, next to its attachments (#576). A copy
    # that fails is named in the post, never retried at the envelope's cost.
    eml_path, auth, body_path = "", None, ""
    try:
        got = await asyncio.to_thread(eml, room, env["id"], str(env.get("key") or ""))
        raw = base64.b64decode(got["content_b64"])
        auth = got.get("auth") if isinstance(got.get("auth"), dict) else None
        eml_path = eml_name(env)
        await topics_client.async_put_file(
            room["tier"], room["name"], eml_path,
            base64.b64encode(raw).decode("ascii"), provenance="untrusted")
    except Exception as e:  # noqa: BLE001
        LOG.warning("mail courier: .eml of %s/%s not saved (%s)",
                    room["tier"], room["name"], type(e).__name__)
        eml_path = ""
        failed.append("messaggio completo (.eml)")
    if eml_path and auth and auth.get("authenticated") is True:
        # #586: a sender the room declares (checked by comms-pack on these
        # very bytes) AND authenticated by the receiving server: its own text
        # is trusted — reading it does not taint. Forwarded parts and
        # attachments stay in the untrusted `.eml` and `files/`.
        text = plain_body(raw)
        if text.strip():
            try:
                body_path = body_name(eml_path)
                await topics_client.async_put_file(
                    room["tier"], room["name"], body_path,
                    base64.b64encode(text.encode("utf-8")).decode("ascii"),
                    provenance="trusted")
            except Exception as e:  # noqa: BLE001
                LOG.warning("mail courier: text of %s/%s not saved (%s)",
                            room["tier"], room["name"], type(e).__name__)
                body_path = ""
    await topics_client.async_post_message(
        room["tier"], room["name"], AUTHOR,
        envelope_text(env, saved, failed, eml_path, auth, body_path), kind="proxy")
    return {"from": _plain(env.get("from")), "subject": _plain(env.get("subject")),
            "eml": eml_path}


async def run_room(room: dict, only: list[str] | None = None,
                   delivered: list | None = None) -> int:
    """One pass over one room's mailbox. Returns how many envelopes were
    delivered. `only`: the keys the message-box index named (#585).
    `delivered`: collects what was delivered, for the room's mail trigger (#628)."""
    state = _load(room)
    # The senders changed (one added, one removed): what is in the mailbox now
    # is recorded, not delivered — a sender added today brings its mail from
    # today, like a mailbox linked today (#576).
    sig = str(room.get("senders_sig") or "")
    if sig != state.get("senders_sig"):
        state["primed"] = False
        state["senders_sig"] = sig
    try:
        out = await asyncio.to_thread(envelopes, room, state["keys"], not state["primed"],
                                      only)
    except requests.HTTPError as e:
        # 403: the room's rules refuse the mailbox now (list changed) — skip.
        LOG.info("mail courier: %s/%s skipped (%s)", room["tier"], room["name"],
                 getattr(e.response, "status_code", "?"))
        return 0
    if not state["primed"]:
        chiavi = [k for k in (out.get("keys") or []) if k]
        if not chiavi and out.get("envelopes"):        # a component without keys_only
            chiavi = [e["key"] for e in out["envelopes"] if e.get("key")]
        state["keys"].extend(chiavi)
        state["primed"] = True
        _save(room, state)
        LOG.info("mail courier: %s/%s primed with %d existing messages",
                 room["tier"], room["name"], len(chiavi))
        return 0
    if only is not None:
        # #585: what comms-pack REFUSED for this room (sender, tag) is seen —
        # searched once, not every cycle. A key it did not reach is retried.
        rifiutate = [k for k in (out.get("withheld_keys") or []) if k in set(only)]
        if rifiutate:
            state["keys"].extend(k for k in rifiutate if k not in state["keys"])
            _save(room, state)
    nuove = list(out.get("envelopes") or [])
    consegnate = 0
    for env in reversed(nuove):           # oldest first
        key = env.get("key")
        try:
            item = await _deliver(room, env)
            consegnate += 1
            if delivered is not None:
                delivered.append(item)
        except Exception as e:  # noqa: BLE001 — retried at the next pass
            LOG.warning("mail courier: envelope for %s/%s not posted (%s)",
                        room["tier"], room["name"], type(e).__name__)
            fails = state.setdefault("fails", {})
            fails[key] = int(fails.get(key, 0)) + 1
            if fails[key] < MAX_ATTEMPTS:
                _save(room, state)
                # Later mail waits: delivery stays in arrival order.
                break
            # Last attempt: the envelope alone, naming what could not be saved.
            try:
                await topics_client.async_post_message(
                    room["tier"], room["name"], AUTHOR,
                    envelope_text(env, [], list(env.get("attachments") or [])),
                    kind="proxy")
                consegnate += 1
                if delivered is not None:
                    delivered.append({"from": _plain(env.get("from")),
                                      "subject": _plain(env.get("subject")), "eml": ""})
            except Exception:  # noqa: BLE001
                LOG.warning("mail courier: envelope for %s/%s given up after %d attempts",
                            room["tier"], room["name"], fails[key])
        state.get("fails", {}).pop(key, None)
        state["keys"].append(key)
        _save(room, state)
    return consegnate


async def run_box_room(room: dict, keys: list[str], delivered: list | None = None) -> int:
    """A message-box row read through the index (#585): no IMAP unless the
    index names keys this room has not seen. Priming records the index's keys
    for the code — nothing is read."""
    state = _load(room)
    sig = str(room.get("senders_sig") or "")
    if sig != state.get("senders_sig") or not state["primed"]:
        state["keys"].extend(k for k in keys if k not in state["keys"])
        state.update(primed=True, senders_sig=sig)
        _save(room, state)
        if keys:
            LOG.info("mail courier: %s/%s primed with %d existing messages",
                     room["tier"], room["name"], len(keys))
        return 0
    nuove = [k for k in keys if k not in state["keys"]]
    if not nuove:
        return 0
    return await run_room(room, only=nuove, delivered=delivered)


async def run_cycle() -> int:
    total = 0
    try:
        stanze = await asyncio.to_thread(rooms)
    except Exception as e:  # noqa: BLE001
        LOG.warning("mail courier: rooms unavailable (%s)", type(e).__name__)
        return 0
    indice = None
    # The room's own address delivers whoever writes (clodia-tools ≥ 2.148.0,
    # #627): an indexed message-box row needs no declared sender.
    if any(r.get("tag") and r.get("indexed") for r in stanze):
        indice = await asyncio.to_thread(box)
    consegnate: dict[tuple[str, str], list] = {}
    for room in stanze:
        box_row = bool(room.get("tag") and room.get("indexed"))
        # A linked mailbox with no sender declared by the room → nothing would
        # be delivered: no IMAP at all (#576). A gateway that does not say
        # counts as none.
        if not box_row and not room.get("senders"):
            continue
        dove = consegnate.setdefault((str(room.get("tier")), str(room.get("name"))), [])
        try:
            if box_row:
                # Without the index this row is skipped, never searched on its
                # own: one search per topic is what #585 removed.
                if indice is not None:
                    total += await run_box_room(
                        room, indice.get(str(room["tag"]).upper(), []), delivered=dove)
                continue
            total += await run_room(room, delivered=dove)
        except Exception as e:  # noqa: BLE001 — one room never stops the others
            LOG.warning("mail courier: %s/%s failed (%s)", room.get("tier"),
                        room.get("name"), type(e).__name__)
    # ONE turn per room per cycle, whatever the number of its mailboxes (#628).
    for (tier, name), items in consegnate.items():
        if items:
            try:
                # The courier never starts a turn itself (no model on its path):
                # the trigger's owner side does, like the scheduler's topic
                # trigger (#628).
                from ..scheduler import mail_triggers
                await mail_triggers.fire(tier, name, items)
            except Exception as e:  # noqa: BLE001 — a trigger never stops the courier
                LOG.warning("mail trigger %s/%s: %s", tier, name, type(e).__name__)
    return total
