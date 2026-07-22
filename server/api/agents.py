"""Endpoint runtime Clodia.

Le chat libere (`/clodia/chats/*`) sono state rimosse: la conversazione 1-1
con un agent è ora un **DM = canale a 2** (vedi `channels.py`). Qui restano
l'helper di identità del principal (riusato da channels/topics) e l'SSE globale
degli eventi (consumato da jobs/colony nel FE)."""
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from sse_starlette.sse import EventSourceResponse

from ..core.events import bus
from ..sdk_runtime.session import manager
from ..agents.workspace import SPAWNS_ROOT

router = APIRouter()
LOG = logging.getLogger("agent-server.api.agents")

# Oltre questo tempo in stato "thinking" senza fine turno → probabile blocco.
_STUCK_AFTER_S = 180


def _topic_of(chat_id: str) -> dict:
    """Deriva il contesto (topic/DM) dal chat_id. I canali sono
    'chan:<tier>:<name>:<responder>'."""
    if chat_id.startswith("chan:"):
        parts = chat_id.split(":")
        if len(parts) >= 4:
            tier, name = parts[1], parts[2]
            return {"topic": f"{tier}/{name}", "kind": "dm" if name.startswith("dm-") else "channel"}
    return {"topic": None, "kind": "chat"}


def _live_status(status: str, last_activity: str) -> str:
    """Mappa lo stato sessione a: running | idle | blocked | stopped."""
    s = (status or "").lower()
    if s in ("idle",):
        return "idle"
    if s in ("stopped",):
        return "stopped"
    if s in ("thinking", "running"):
        try:
            la = datetime.fromisoformat(last_activity)
            age = (datetime.now(timezone.utc) - la).total_seconds()
            return "blocked" if age > _STUCK_AFTER_S else "running"
        except Exception:  # noqa: BLE001
            return "running"
    return s or "unknown"


def _spawn_rows() -> list[dict]:
    rows: list[dict] = []
    if not SPAWNS_ROOT.is_dir():
        return rows
    for d in sorted(SPAWNS_ROOT.iterdir()):
        if not d.is_dir():
            continue
        agent, sep, instance = d.name.rpartition("-")
        if not sep:
            agent, instance = d.name, None
        try:
            last_activity = datetime.fromtimestamp(d.stat().st_mtime, timezone.utc).isoformat()
        except OSError:
            last_activity = None
        rows.append({
            "chat_id": f"spawn:{d.name}",
            "agent": agent,
            "spawn_id": d.name,
            "spawn_instance": instance,
            "runtime": None,
            "principal": None,
            "topic": None,
            "context_kind": "spawn",
            "state": "idle",
            "last_activity": last_activity,
            "created_at": last_activity,
            "tokens_in": 0,
            "tokens_out": 0,
            "runs": 0,
        })
    return rows


@router.get("/clodia/runtime/sessions")
async def runtime_sessions() -> dict:
    """Vista 'top'/Activity Monitor: agenti spawnati con topic, token, stato."""
    rows = []
    seen_spawns = set()
    for c in manager.list():
        d = c.to_dict()
        ctx = _topic_of(d["chat_id"])
        tot = d.get("total_tokens") or {}
        if d.get("spawn_id"):
            seen_spawns.add(d["spawn_id"])
        rows.append({
            "chat_id": d["chat_id"],
            "agent": d["kind"],
            "spawn_id": d.get("spawn_id"),
            "spawn_instance": d.get("spawn_instance"),
            "runtime": d.get("runtime"),
            "principal": d.get("principal"),
            "topic": ctx["topic"],
            "context_kind": ctx["kind"],
            "state": _live_status(d.get("status", ""), d.get("last_activity", "")),
            "last_activity": d.get("last_activity"),
            "created_at": d.get("created_at"),
            "tokens_in": tot.get("input", 0),
            "tokens_out": tot.get("output", 0),
            "runs": tot.get("runs", 0),
        })
    for row in _spawn_rows():
        if row["spawn_id"] not in seen_spawns:
            rows.append(row)
    return {"sessions": rows}


@router.post("/clodia/runtime/restart-agent")
async def runtime_restart_agent(body: dict) -> dict:
    """Restart mirato delle sessioni vive di un agente (per sbloccarlo se il
    runtime si impunta). La history persiste: al prossimo messaggio la chat
    rimaterializza il seed da zero. Verbo di ops di sysadmin (via gateway
    runtime.restart_agent)."""
    agent = str((body or {}).get("agent") or "").strip()
    if not agent:
        return {"ok": False, "error": "agent mancante"}
    stopped = await manager.drop_agent(agent)
    return {"ok": True, "agent": agent, "restarted": stopped,
            "count": len(stopped)}


def _principal_from_request(request: Request) -> str | None:
    """Estrae e VERIFICA il principal umano dal session token (Bearer ckt1)
    della webui. Ritorna il nome del principal (firma validata dalla CA) o None
    se assente/non valido. Non blocca: l'identità è additiva (F2a)."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        LOG.info("principal: nessun Bearer nell'header (anonimo)")
        return None
    token = auth[7:].strip()
    try:
        from ..colony import pki
        payload = pki.verify_session_token(token)
        p = payload.get("agent") or None
        LOG.info("principal: token verificato → %s", p)
        return p
    except Exception as e:  # noqa: BLE001 — token assente/scaduto/non valido → anonimo
        LOG.warning("principal: verifica token fallita: %s", e)
        return None


@router.get("/clodia/events")
async def events():
    """SSE globale: tutti gli eventi di tutte le chat, ogni evento porta chat_id nel payload."""
    async def event_stream():
        async for ev in bus.subscribe():
            yield {"data": json.dumps(ev.model_dump(), default=str)}
    return EventSourceResponse(event_stream())
