"""Relay Telegram → topic (modello telegram-proxy corretto, 18 lug 2026).

Binding sull'ISTANZA del messaggero (`telegram-bindings.json`, scritto dai verbi
telegram.listen/unlisten del gateway), NON nel meta del topic. Il relay itera i
BINDING, non i topic.

Comportamento (deciso con Davide):
- il messaggero OSSERVA la chat e ne tiene un BUFFER di contesto (verbatim + handle
  autenticati), ma NON riversa ogni messaggio nel topic;
- si ATTIVA solo quando un messaggio **interpella il bot** (menzione @clodia*/agente):
  * mittente fra gli INGRESS del topic (`tg:@handle`) → riporta nel topic il
    **contesto accumulato + la richiesta** (un blocco unico, autore = istanza
    messaggero), poi innesca il responder tra gli agenti reali;
  * mittente NON vagliato → il **messaggero risponde su Telegram** col rifiuto
    «Non sono autorizzata ad interagire con questo utente»; NON tocca il topic;
- la chiacchiera che non interpella il bot resta nel buffer (contesto), non entra
  da sola nel topic.

L'autorizzazione del mittente è un INGRESS dello scope come ogni altro
(clodia-platform#365): fino al 14 set 2026 viveva in un blocco JSON ad-hoc nella
`MEMORY.md` del messaggero, invisibile al modello ingress/egress e non revocabile
dall'owner con l'interfaccia standard. Con lei è sparita la distinzione
`command`/`dialogue`, che nel codice non aveva mai avuto un effetto distinto.

Trasporto MECCANICO: nessuna logica AI nel relay.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from ..config import data_path
from . import telegram_bindings_client as tb
from . import telegram_client, topics_client
from .channels import run_topic_turn

LOG = logging.getLogger("agent-server.channel_relay")

_SEEN_CAP = 500
_BUFFER_CAP = 40   # finestra di contesto massima per chat
_DENY = "Mi spiace ma non sono autorizzata ad interagire con te"


def _is_messenger(agent: str) -> bool:
    a = str(agent or "")
    return a == "messaggero" or a.startswith("messaggero-")


# ── autorizzazione del mittente: un INGRESS dello scope come ogni altro ───────
async def _is_vetted_tg_source(username, tier: str, topic: str) -> bool:
    """True se `@username` è una fonte vagliata PER QUESTO topic.

    Chi decide è il gateway (`egress.is_vetted_source`), interrogato con la query
    di appartenenza `/internal/egress?uri=…&direction=ingress&scope=…`: la lista
    vive sul volume che l'agent-server non monta di proposito (clodia-platform#80)
    e la regola di match non è banale (wildcard di schema, liste per-scope,
    perimetro). Rifarla qui sarebbe una seconda copia che diverge alla prima
    modifica, e divergerebbe in silenzio — un'autorizzazione concessa per sbaglio
    non la rilegge nessuno.

    Lo scope è quello del BINDING, non del chiamante: la chat è legata a QUEL
    topic, e vagliare contro le fonti di un altro sbaglierebbe nella direzione
    permissiva (stessa ragione di clodia-platform#364).

    **Fail-closed** su gateway irraggiungibile o risposta non leggibile: un
    guasto non autorizza nessuno. E un mittente senza handle non è vagliabile per
    costruzione — `tg:` in ingresso registra `@handle`, non un uid — quindi è
    rifiutato qui senza nemmeno chiedere, invece di esplodere più in basso.
    """
    handle = str(username or "").strip().lstrip("@")
    if not handle:
        return False
    from .observe import _gw
    try:
        r = await asyncio.to_thread(
            _gw, "/internal/egress",
            {"uri": f"tg:@{handle}", "direction": "ingress", "scope": f"{tier}/{topic}"})
        r.raise_for_status()
        return bool(r.json().get("vetted"))
    except Exception as e:  # noqa: BLE001
        LOG.warning("ingress di %s/%s per tg:@%s non verificabile (%s) → rifiuto",
                    tier, topic, handle, str(e)[:120])
        return False


def _addresses_bot(text: str, participants: list) -> bool:
    """True se il messaggio INTERPELLA il bot o un agente del topic (menzione)."""
    t = (text or "").lower()
    if "@clodia" in t:
        return True
    for p in (participants or []):
        pl = str(p).lower()
        if pl and f"@{pl}" in t:
            return True
    return False


# ── stato per-chat: seen (dedup) + buffer di contesto ─────────────────────────
def _state_path(chat_id: str):
    d = data_path("channel-relay-state")
    d.mkdir(parents=True, exist_ok=True)
    safe = str(chat_id).replace("/", "_")
    return d / f"chat_{safe}.json"


def _load_state(chat_id: str) -> dict:
    p = _state_path(chat_id)
    if not p.is_file():
        return {"seen": [], "buffer": []}
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
        s.setdefault("seen", [])
        s.setdefault("buffer", [])
        return s
    except (OSError, json.JSONDecodeError):
        return {"seen": [], "buffer": []}


def _save_state(chat_id: str, state: dict) -> None:
    state["seen"] = state.get("seen", [])[-_SEEN_CAP:]
    state["buffer"] = state.get("buffer", [])[-_BUFFER_CAP:]
    _state_path(chat_id).write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _safe_filename(name: str) -> str:
    """Nome file sicuro per lo storage del topic (no path traversal)."""
    base = os.path.basename(str(name or "file")).replace("\\", "_").strip() or "file"
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)[:120]


# ── rendering del contesto (compatto) ─────────────────────────────────────────
def _line(m: dict, chat_id: str) -> str:
    """Una riga compatta per messaggio: `[tg://<gruppo>/<user>] -> <verbatim>`.
    `<gruppo>` = NOME/titolo della chat (leggibile), fallback al chat_id.
    `<user>` = username Telegram (identità autenticata) o uid se assente.
    Se il messaggio ha un allegato, aggiunge il riferimento al file salvato."""
    group = m.get("chat_title") or chat_id
    user = m.get("from_username") or (str(m.get("from_id")) if m.get("from_id") is not None else "?")
    text = (m.get("text") or "").strip()
    f = m.get("file")
    if f:
        saved = m.get("saved_file")
        tag = (f"📎 {f.get('file_name')} → salvato in `{saved}` (leggilo con "
               f"topic.read_document/read_file)" if saved
               else f"📎 {f.get('file_name')} (download non riuscito)")
        text = f"{text} {tag}".strip() if text else tag
    return f"[tg://{group}/{user}] -> {text}"


def _context_block(buffer: list, chat_id: str) -> str:
    """Blocco compatto: una riga per messaggio del buffer (contesto + richiesta).
    Le istruzioni comportamentali stanno una volta sola in _CHANNEL_CAPS, non qui."""
    return "\n".join(_line(m, chat_id) for m in buffer if (m.get("text") or "").strip())


# ── relay di una singola chat legata (binding) ────────────────────────────────
async def _relay_chat(chat_id: str, binding: dict, messages: list) -> None:
    instance = binding.get("instance") or "messaggero"
    tier = binding.get("tier")
    topic = binding.get("topic")
    if not (tier and topic):
        return
    try:
        meta = (await topics_client.async_open_topic(tier, topic)).get("meta", {})
    except Exception as e:  # noqa: BLE001
        LOG.warning("open_topic %s/%s: %s", tier, topic, e)
        return
    participants = meta.get("participants") or []

    state = _load_state(chat_id)
    seen = set(state.get("seen", []))
    buffer = state.get("buffer", [])

    trigger = None          # ultimo messaggio LEGIT che interpella il bot
    for m in messages:
        mid = m.get("message_id")
        if mid in seen:
            continue
        seen.add(mid)
        state["seen"].append(mid)
        text = (m.get("text") or "").strip()
        if not text:
            continue
        buffer.append(m)                       # contesto (sempre)
        # Il bot è INTERPELLATO da una menzione (@clodia/agente) OPPURE da una
        # REPLY a un suo messaggio (le reply valgono come menzioni dirette).
        if not (_addresses_bot(text, participants) or m.get("reply_to_bot")):
            continue
        # messaggio che INTERPELLA il bot → PRIMO CHECK: mittente vagliato come
        # fonte di QUESTO topic? L'handle è quello AUTENTICATO (campo `from`
        # dell'API), mai ciò che il testo dichiara.
        disp = m.get("from") or m.get("from_username") or str(m.get("from_id"))
        if await _is_vetted_tg_source(m.get("from_username"), tier, topic):
            # SÌ → ACK immediato su Telegram + riporto nel topic (trigger)
            trigger = m
            try:
                await telegram_client.send_async(
                    str(chat_id),
                    f"✅ Ricevuto, {disp}. Prendo in carico e porto il messaggio nel topic.")
            except Exception as e:  # noqa: BLE001
                LOG.warning("ack send chat %s: %s", chat_id, e)
        else:
            # NO → deny immediato su Telegram (non tocca il topic)
            try:
                await telegram_client.send_async(str(chat_id), _DENY)
            except Exception as e:  # noqa: BLE001
                LOG.warning("deny send chat %s: %s", chat_id, e)

    state["buffer"] = buffer
    if trigger is not None:
        # Allegati: scarica da Telegram e salvali nello storage del topic, così gli
        # agenti li leggono con topic.read_document. Solo alla relay (on trigger).
        for m in buffer:
            f = m.get("file")
            if not f or m.get("saved_file") is not None:
                continue
            try:
                dl = await telegram_client.download_async(f["file_id"])
                fname = _safe_filename(f.get("file_name") or f["file_id"])
                await topics_client.async_put_file(tier, topic, fname, dl["content_b64"])
                m["saved_file"] = f"files/{fname}"
            except Exception as e:  # noqa: BLE001
                LOG.warning("download/save file %s/%s: %s", tier, topic, e)
                m["saved_file"] = ""      # download non riuscito (marcato)
        block = _context_block(buffer, str(chat_id))
        try:
            await topics_client.async_post_message(tier, topic, instance, block, kind="telegram")
            state["buffer"] = []               # contesto consumato → svuota
        except Exception as e:  # noqa: BLE001
            LOG.warning("post_message %s/%s: %s", tier, topic, e)
        else:
            meta_turn = dict(meta)
            meta_turn["participants"] = [p for p in participants if not _is_messenger(p)]
            try:
                await run_topic_turn(tier, topic, meta_turn,
                                     trigger_text=(trigger.get("text") or ""))
            except Exception as e:  # noqa: BLE001
                LOG.warning("responder turn %s/%s: %s", tier, topic, e)

    _save_state(chat_id, state)


async def run_poll_cycle(timeout: int = 25) -> int:
    """UN ciclo di long-poll: blocca (in un thread) fino a un nuovo messaggio o al
    timeout, poi instrada i messaggi delle chat LEGATE ai rispettivi topic. Ritorna
    il numero di chat servite. Latenza quasi zero: appena arriva un messaggio, il
    getUpdates ritorna e si processa subito."""
    updates = await asyncio.to_thread(telegram_client.poll, timeout)
    if not updates:
        return 0
    bindings = tb.load()
    by_chat: dict = {}
    for u in updates:
        by_chat.setdefault(str(u.get("chat_id")), []).append(u)
    n = 0
    for chat_id, msgs in by_chat.items():
        b = bindings.get(chat_id)
        if not b:            # chat non legata a nessun topic → ignora
            continue
        try:
            await _relay_chat(chat_id, b, msgs)
            n += 1
        except Exception as e:  # noqa: BLE001
            LOG.warning("relay chat %s: %s", chat_id, e)
    return n
