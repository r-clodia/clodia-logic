"""Segnali per-principal dei recent topics (issue clodia-platform#83).

Due segnali con gerarchia esplicita, calcolati SERVER-SIDE per la coppia
(principal, topic) — mai condivisi fra utenti (I1):

- **actionable** (badge numerico): mention non lette rivolte al principal
  (campo strutturato `mentions` del messaggio, scritto dal gateway al
  write-time — D1) + gate di workflow pendenti assegnati a lui (D2:
  `wf_owner`/`requested_by`, stessa regola di notify.py). Conta gli item,
  non i messaggi.
- **activity** (pallino booleano): esiste almeno un messaggio successivo
  all'ultima visita non scritto dal principal. Nessuna gradazione.

Regole di spegnimento: la visita (POST seen) azzera SOLO activity. Le
mention si spengono con l'ack esplicito `mentions_upto` (il client lo manda
quando la coda dei messaggi è stata effettivamente renderizzata, non alla
mera navigazione). I gate NON si spengono mai per lettura: escono dal
conteggio solo quando il run non è più `gate_pending` (risolto) o cambia
assegnatario (riassegnato).

Isolamento: la membership è rivalutata a OGNI chiamata sul meta corrente
del topic (I3 — nessun verdetto cachato: revoca → il badge sparisce al
refetch successivo). I topic di cui il principal non è participant sono
OMESSI dalla risposta, nemmeno un conteggio a zero (I2). Se la membership è
confermata ma i messaggi non sono leggibili, si segnala activity (I4:
fail-safe verso il mostrare).

Lo stato di lettura è per-principal in
`user-settings/<principal>/topic-read.json` (stesso pattern isolato di
channel-aliases).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..config import data_path
from ..workflows import store as wf_store
from . import topics_client
from .agents import _principal_from_request

router = APIRouter()
LOG = logging.getLogger("agent-server.api.topic_signals")

_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TOPIC_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}/[A-Za-z0-9._-]{1,128}$")
_MAX_TOPICS = 20          # la sidebar ne mostra 5: bound difensivo
_MSG_WINDOW = 200         # finestra messaggi per il conteggio


def _principal(request: Request) -> str:
    principal = _principal_from_request(request)
    if not principal or not _PRINCIPAL_RE.fullmatch(principal):
        raise HTTPException(401, "login richiesto")
    return principal


def _state_path(principal: str) -> Path:
    return data_path("user-settings") / principal / "topic-read.json"


def _load_state(principal: str) -> dict:
    try:
        raw = json.loads(_state_path(principal).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(principal: str, state: dict) -> None:
    path = _state_path(principal)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _after(msg_ts: str | None, marker: str | None) -> bool:
    """True se il messaggio è successivo al marker (o il confronto è incerto:
    direzione fail-safe I4 — nel dubbio il messaggio conta)."""
    m, mk = _ts(msg_ts), _ts(marker)
    if mk is None:
        return True
    if m is None:
        return True
    return m > mk


def _gate_assignee(run: dict) -> str:
    """Assegnatario del gate: stessa regola delle notifiche (notify.py)."""
    return (run.get("wf_owner") or "").strip() or (run.get("requested_by") or "")


def _pending_gates_for(principal: str) -> dict[str, int]:
    """Mappa 'tier/name' → numero di gate pendenti assegnati al principal."""
    out: dict[str, int] = {}
    try:
        runs = wf_store.list_runs(include_done=False)
    except Exception:  # noqa: BLE001 — lo store non deve rompere i segnali
        return out
    for run in runs:
        if not run.get("gate_pending"):
            continue
        topic = run.get("topic") or {}
        tier, name = topic.get("tier"), topic.get("name")
        if not tier or not name:
            continue
        if _gate_assignee(run) != principal:
            continue
        key = f"{tier}/{name}"
        out[key] = out.get(key, 0) + 1
    return out


def _topic_signal(principal: str, tier: str, name: str,
                  read: dict, gates: int) -> dict | None:
    """Segnale (actionable, activity) di UN topic per il principal, o None se
    il principal non è participant/owner (I2: omesso, non zero)."""
    topic = topics_client.open_topic(tier, name)
    if not topic:
        return None
    meta = topic.get("meta", {})
    # I3: autorizzazione rivalutata qui, a ogni chiamata, sul meta corrente.
    if principal != meta.get("owner") and principal not in meta.get("participants", []):
        return None
    visited = read.get("visited")
    mentions_upto = read.get("mentions_upto")
    try:
        messages = topics_client.list_messages(tier, name, limit=_MSG_WINDOW)
    except Exception:  # noqa: BLE001
        # Membership confermata ma contenuti non leggibili → I4: si segnala.
        return {"actionable": gates, "activity": True}
    unread_mentions = 0
    activity = False
    for msg in messages:
        if msg.get("author") == principal:
            continue
        ts = msg.get("ts")
        if _after(ts, visited):
            activity = True
        if principal.lower() in (msg.get("mentions") or []) and _after(ts, mentions_upto):
            unread_mentions += 1
    return {"actionable": unread_mentions + gates, "activity": activity}


@router.get("/api/topics/signals")
async def topic_signals(request: Request, topics: str = "") -> dict:
    """Segnali dei topic richiesti (query `topics=tier/name,...`) per il
    principal corrente. I topic non accessibili sono semplicemente assenti."""
    principal = _principal(request)
    keys: list[str] = []
    for raw in (topics or "").split(","):
        key = raw.strip()
        if key and _TOPIC_KEY_RE.fullmatch(key) and key not in keys:
            keys.append(key)
    keys = keys[:_MAX_TOPICS]
    state = _load_state(principal)
    gates = _pending_gates_for(principal)
    signals: dict[str, dict] = {}
    for key in keys:
        tier, name = key.split("/", 1)
        try:
            sig = _topic_signal(principal, tier, name,
                                state.get(key) or {}, gates.get(key, 0))
        except Exception as e:  # noqa: BLE001 — un topic rotto non oscura gli altri
            LOG.warning("signals %s: %s", key, str(e)[:120])
            continue
        if sig is not None:
            signals[key] = sig
    return {"signals": signals}


@router.post("/api/topics/{tier}/{name}/seen")
async def topic_seen(tier: str, name: str, request: Request) -> dict:
    """Registra la visita del principal al topic → spegne il pallino
    (activity). Con `mentions_upto` nel body registra anche l'ack di lettura
    delle mention fino a quel timestamp (il client lo manda solo quando la
    coda dei messaggi è stata renderizzata). Non tocca MAI i gate."""
    principal = _principal(request)
    topic = topics_client.open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "topic non trovato")
    meta = topic.get("meta", {})
    if principal != meta.get("owner") and principal not in meta.get("participants", []):
        raise HTTPException(403, "non sei partecipante di questo topic")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — body opzionale
        body = {}
    key = f"{tier}/{name}"
    state = _load_state(principal)
    entry = state.get(key) or {}
    entry["visited"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    upto = _ts(str(body.get("mentions_upto") or "")) if isinstance(body, dict) else None
    if upto is not None:
        prev = _ts(entry.get("mentions_upto"))
        if prev is None or upto > prev:
            entry["mentions_upto"] = upto.isoformat(timespec="seconds")
    state[key] = entry
    _save_state(principal, state)
    return {"ok": True, "topic": key, "read": entry}
