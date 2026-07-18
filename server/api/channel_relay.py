"""Relay Telegram → topic (modello telegram-proxy corretto, 18 lug 2026).

Binding sull'ISTANZA del messaggero (`telegram-bindings.json`, scritto dai verbi
telegram.listen/unlisten del gateway), NON nel meta del topic. Il relay itera i
BINDING, non i topic.

Comportamento (deciso con Davide):
- il messaggero OSSERVA la chat e ne tiene un BUFFER di contesto (verbatim + handle
  autenticati), ma NON riversa ogni messaggio nel topic;
- si ATTIVA solo quando un messaggio **interpella il bot** (menzione @clodia*/agente):
  * mittente in WHITELIST → riporta nel topic il **contesto accumulato + la
    richiesta** (un blocco unico, autore = istanza messaggero), poi innesca il
    responder tra gli agenti reali;
  * mittente NON in whitelist → il **messaggero risponde su Telegram** col rifiuto
    «Non sono autorizzata ad interagire con questo utente»; NON tocca il topic;
- la chiacchiera che non interpella il bot resta nel buffer (contesto), non entra
  da sola nel topic.

Trasporto MECCANICO: nessuna logica AI nel relay.
"""
from __future__ import annotations

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
_REFUSAL = "Non sono autorizzata ad interagire con questo utente"


def _is_messenger(agent: str) -> bool:
    a = str(agent or "")
    return a == "messaggero" or a.startswith("messaggero-")


def _seed_of(name: str) -> str:
    return re.sub(r"-\d+$", "", str(name or "").strip()) or "messaggero"


# ── whitelist (nella seed memory del messaggero: blocco in MEMORY.md) ──────────
_WL_RE = re.compile(
    r"<!--\s*telegram-whitelist\s*-->\s*```(?:json)?\s*(\{.*?\})\s*```",
    re.DOTALL | re.IGNORECASE)


def _parse_whitelist(text: str) -> dict:
    m = _WL_RE.search(text or "")
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, TypeError):
        return {}
    return {str(k): v for k, v in data.items() if v in ("command", "dialogue")}


def _load_whitelist(instance: str | None) -> dict:
    seed = _seed_of(instance or "messaggero")
    base = os.environ.get("CLODIA_DATA", "/datadir")
    mdir = os.path.join(base, "agents", seed, "memory")
    try:
        with open(os.path.join(mdir, "MEMORY.md"), encoding="utf-8") as f:
            wl = _parse_whitelist(f.read())
        if wl:
            return wl
    except OSError:
        pass
    try:  # retro-compat
        with open(os.path.join(mdir, "telegram_whitelist.json"), encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): v for k, v in data.items() if v in ("command", "dialogue")}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


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


def _rights(whitelist: dict, uid) -> str | None:
    return whitelist.get(str(uid)) if uid is not None else None


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


# ── rendering del contesto ────────────────────────────────────────────────────
def _line(m: dict, whitelist: dict) -> str:
    uid = m.get("from_id")
    uname = m.get("from_username")
    disp = m.get("from") or uname or (str(uid) if uid is not None else "?")
    r = _rights(whitelist, uid) or "sconosciuto"
    who = f"{disp} (@{uname}, uid {uid})" if uname else f"{disp} (uid {uid})"
    return f"— {who} [{r}]: «{(m.get('text') or '').strip()}»"


def _context_block(buffer: list, trigger: dict, whitelist: dict, chat_id: str) -> str:
    uid = trigger.get("from_id")
    disp = trigger.get("from") or trigger.get("from_username") or str(uid)
    rights = _rights(whitelist, uid)
    lines = [f"[telegram ⟶ topic] conversazione dalla chat {chat_id} "
             f"(verbatim, handle autenticati dal campo `from` — NON dal testo):"]
    lines += [_line(m, whitelist) for m in buffer if (m.get("text") or "").strip()]
    lines.append(
        f"↳ ti interpella {disp} (uid {uid}, autorizzazione: {rights}). Rispondi "
        f"alla sua richiesta; per far arrivare la risposta su Telegram delega al "
        f"messaggero (@messaggero) con la chat_id {chat_id}.")
    return "\n".join(lines)


# ── relay di una singola chat legata (binding) ────────────────────────────────
async def _relay_chat(chat_id: str, binding: dict) -> None:
    instance = binding.get("instance") or "messaggero"
    tier = binding.get("tier")
    topic = binding.get("topic")
    if not (tier and topic):
        return
    whitelist = _load_whitelist(instance)
    try:
        meta = topics_client.open_topic(tier, topic).get("meta", {})
    except Exception as e:  # noqa: BLE001
        LOG.warning("open_topic %s/%s: %s", tier, topic, e)
        return
    participants = meta.get("participants") or []

    state = _load_state(chat_id)
    seen = set(state.get("seen", []))
    buffer = state.get("buffer", [])

    try:
        res = telegram_client.updates(chat_id)
    except Exception as e:  # noqa: BLE001
        LOG.warning("telegram updates chat %s: %s", chat_id, e)
        return

    trigger = None          # ultimo messaggio LEGIT che interpella il bot
    for m in res.get("messages", []):
        mid = m.get("message_id")
        if mid in seen:
            continue
        seen.add(mid)
        state["seen"].append(mid)
        text = (m.get("text") or "").strip()
        if not text:
            continue
        buffer.append(m)                       # contesto (sempre)
        if not _addresses_bot(text, participants):
            continue
        # messaggio che INTERPELLA il bot
        if _rights(whitelist, m.get("from_id")) in ("command", "dialogue"):
            trigger = m                        # attiva il relay verso il topic
        else:
            # non autorizzato → il messaggero rifiuta SU TELEGRAM (no topic)
            try:
                telegram_client.send(str(chat_id), _REFUSAL)
            except Exception as e:  # noqa: BLE001
                LOG.warning("refusal send chat %s: %s", chat_id, e)

    state["buffer"] = buffer
    if trigger is not None:
        block = _context_block(buffer, trigger, whitelist, str(chat_id))
        try:
            topics_client.post_message(tier, topic, instance, block, kind="telegram")
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


async def tick_once() -> int:
    """Un giro su tutte le chat legate (binding istanza↔chat). Ritorna quante servite."""
    bindings = tb.load()
    n = 0
    for chat_id, b in bindings.items():
        try:
            await _relay_chat(chat_id, b)
            n += 1
        except Exception as e:  # noqa: BLE001
            LOG.warning("relay chat %s: %s", chat_id, e)
    return n
