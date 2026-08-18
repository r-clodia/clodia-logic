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

Due dettagli servono a non spegnere un badge che deve restare acceso (I4:
il falso negativo è l'esito peggiore, un item che aspetta qualcuno e che
nessuno vede):

- **Copertura della finestra.** I messaggi si leggono a finestra
  (`_MSG_WINDOW`). Se la finestra è piena, non arriva fino all'ack e non ha
  trovato mention, il conteggio verrebbe dato per 0 senza aver guardato
  dove le mention non lette potrebbero stare: in quel caso — e SOLO in
  quello, per non pagare la finestra larga a ogni richiesta — si rilegge
  fino a `_MSG_WINDOW_MAX`.
- **Granularità dell'ack.** I `ts` hanno risoluzione al secondo, quindi
  «letto fino a `mentions_upto`» non basta a distinguere i messaggi che
  cadono nello stesso secondo dell'ack: quelli arrivati dopo passerebbero
  per letti senza essere mai stati mostrati. L'ack registra perciò anche
  gli id del secondo di bordo (`mentions_acked`); tutto ciò che sta in quel
  secondo e non è in elenco resta non letto.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..config import data_path
from . import topics_client
from .agents import _principal_from_request

router = APIRouter()
LOG = logging.getLogger("agent-server.api.topic_signals")

_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TOPIC_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}/[A-Za-z0-9._-]{1,128}$")
_MAX_TOPICS = 20          # la sidebar ne mostra 5: bound difensivo
_MSG_WINDOW = 200         # finestra messaggi per il conteggio
_MSG_WINDOW_MAX = 2000    # finestra estesa: solo quando il badge sarebbe 0 non coperto
_EDGE_IDS_MAX = 50        # id conservati per il secondo di bordo dell'ack


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


def _mention_unread(msg: dict, upto: str | None, acked: list) -> bool:
    """True se la mention contenuta nel messaggio è ancora da leggere.

    Il confronto col solo `upto` non basta: i `ts` sono al secondo, quindi un
    messaggio arrivato nello stesso secondo dell'ack — ma DOPO — risulterebbe
    letto senza essere mai stato mostrato. Nel secondo di bordo vale l'elenco
    di id effettivamente acked; fuori da quel secondo il confronto ordinario.
    """
    m, mk = _ts(msg.get("ts")), _ts(upto)
    if mk is None or m is None:      # mai letto / ts illeggibile → I4: conta
        return True
    if m > mk:
        return True
    if m == mk:
        return str(msg.get("id") or "") not in acked
    return False


def _covers(messages: list, marker: str | None) -> bool:
    """True se la finestra `messages` risale almeno fino al marker: solo in
    quel caso «nessuna mention non letta» è un'affermazione verificata e non
    un limite della finestra."""
    mk = _ts(marker)
    if mk is None:
        return False
    stamps = [t for t in (_ts(m.get("ts")) for m in messages) if t is not None]
    return bool(stamps) and min(stamps) <= mk


def _scan(messages: list, principal: str, visited: str | None,
          upto: str | None, acked: list) -> tuple[int, bool]:
    """(mention non lette, activity) su una finestra di messaggi."""
    unread, activity = 0, False
    for msg in messages:
        if msg.get("author") == principal:
            continue
        if _after(msg.get("ts"), visited):
            activity = True
        if principal.lower() in (msg.get("mentions") or []) and \
                _mention_unread(msg, upto, acked):
            unread += 1
    return unread, activity


def _edge_ids(tier: str, name: str, upto: datetime) -> list[str]:
    """Id dei messaggi che cadono esattamente nel secondo dell'ack: sono i
    soli, in quel secondo, che l'utente ha davvero visto."""
    try:
        messages = topics_client.list_messages(tier, name, limit=_MSG_WINDOW)
    except Exception:  # noqa: BLE001 — l'ack non deve fallire per questo
        return []
    ids = [str(m["id"]) for m in messages
           if m.get("id") and _ts(m.get("ts")) == upto]
    return ids[-_EDGE_IDS_MAX:]


def _pending_gates_for(principal: str) -> dict[str, int]:
    """Mappa 'tier/name' → gate pendenti in quella stanza che TOCCA a `principal`.

    La fonte è cambiata il 9 ago 2026. Prima era lo store dei workflow: il
    badge contava i gate di un run. Rimossi i workflow, quel conteggio sarebbe
    rimasto a zero per sempre — un badge dichiarato che nessuno alimenta, cioè
    il difetto che questa settimana ho trovato sette volte. Ora la fonte è il
    GATEWAY, che è dove i gate vivono davvero.

    Chi decide viene dalla stessa regola dei gate (voce 24): walls e outward li
    sblocca l'owner dello scope, gli altri un admin. Qui basta la stanza: il
    badge dice «c'è qualcosa che aspetta te», e chiedere al gateway di
    rivalutare il titolo per ogni topic della lista costerebbe una chiamata per
    topic per mostrare un pallino.

    Fallisce in silenzio e ritorna vuoto: un badge è un aiuto, e un aiuto che
    rompe la pagina quando il gateway tossisce non è un aiuto.
    """
    out: dict[str, int] = {}
    try:
        from . import gate as _gate
        r = _gate._gw("GET", "/pending", principal)
        if r.status_code >= 400:
            return out
        richieste = (r.json() or {}).get("requests", [])
    except Exception as e:  # noqa: BLE001 — i segnali non devono rompersi
        LOG.warning("gate pendenti non leggibili: %s", str(e)[:120])
        return out
    for req in richieste:
        chat = req.get("chat") or ""
        if not chat.startswith("chan:"):
            continue
        parti = chat.split(":")
        if len(parti) < 3:
            continue
        key = f"{parti[1]}/{parti[2]}"
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
    acked = [str(i) for i in (read.get("mentions_acked") or [])]
    try:
        messages = topics_client.list_messages(tier, name, limit=_MSG_WINDOW)
    except Exception:  # noqa: BLE001
        # Membership confermata ma contenuti non leggibili → I4: si segnala.
        return {"actionable": gates, "activity": True}
    unread_mentions, activity = _scan(messages, principal, visited, mentions_upto, acked)
    covered = len(messages) < _MSG_WINDOW or _covers(messages, mentions_upto)
    if unread_mentions == 0 and not covered:
        # Il badge starebbe per spegnersi senza che la finestra abbia davvero
        # guardato dove le mention non lette possono stare: prima di dire 0 si
        # allarga (I4). Costo pagato solo qui, non a ogni richiesta.
        try:
            wide = topics_client.list_messages(tier, name, limit=_MSG_WINDOW_MAX)
        except Exception:  # noqa: BLE001
            return {"actionable": gates, "activity": True}
        unread_mentions, wide_activity = _scan(wide, principal, visited,
                                               mentions_upto, acked)
        activity = activity or wide_activity
        if unread_mentions == 0 and len(wide) >= _MSG_WINDOW_MAX and \
                not _covers(wide, mentions_upto):
            LOG.warning("signals %s/%s: finestra %d messaggi non copre l'ack di %s "
                        "— eventuali mention più vecchie non sono conteggiate",
                        tier, name, _MSG_WINDOW_MAX, principal)
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
    gates = await asyncio.to_thread(_pending_gates_for, principal)
    signals: dict[str, dict] = {}
    for key in keys:
        tier, name = key.split("/", 1)
        try:
            sig = await asyncio.to_thread(_topic_signal, principal, tier, name,
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
    topic = await topics_client.async_open_topic(tier, name)
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
            # Il secondo di bordo va registrato per id: i ts sono al secondo e
            # ciò che arriva nello stesso secondo, dopo l'ack, non è stato letto.
            entry["mentions_acked"] = await asyncio.to_thread(_edge_ids, tier, name, upto)
    state[key] = entry
    _save_state(principal, state)
    return {"ok": True, "topic": key, "read": entry}
