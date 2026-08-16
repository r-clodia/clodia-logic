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

from starlette.responses import JSONResponse

from ..agents import registry
from ..core.events import bus
from . import topics_client
from ..sdk_runtime.session import manager
from ..agents.workspace import SPAWNS_ROOT
from ..sdk_runtime.process_reaper import runtime_process_metrics

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
    return {
        "sessions": rows,
        "metrics": {
            "managed_sessions": len(manager.list()),
            **runtime_process_metrics(),
        },
    }


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


#: Meta delle stanze, per decidere la visibilità di un evento senza rileggere il
#: topic a ogni battito. TTL corto: un partecipante appena tolto non deve
#: continuare a ricevere per minuti.
_ROOM_META_TTL = 20.0
_room_meta_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def _room_meta(tier: str, name: str) -> dict | None:
    import time as _time

    key = (tier, name)
    hit = _room_meta_cache.get(key)
    now = _time.monotonic()
    if hit and now - hit[0] < _ROOM_META_TTL:
        return hit[1]
    try:
        meta = (topics_client.open_topic(tier, name) or {}).get("meta") or {}
    except Exception:  # noqa: BLE001 — stanza illeggibile → nessuna consegna
        return None
    _room_meta_cache[key] = (now, meta)
    return meta


def _stream_principal(request: Request) -> tuple[str | None, bool]:
    """Chi sta ascoltando, e se è un proxy. `(principal, is_proxy)`.

    Il token arriva dall'header quando il client è un programma, e dalla query
    quando è un browser: `EventSource` non sa mandare header, e senza questa
    seconda via l'endpoint resterebbe aperto — che è esattamente com'era.
    """
    from ..colony import pki

    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        token = (request.query_params.get("token") or "").strip()
    if not token:
        return None, False
    try:
        payload = pki.verify_session_token(token)
    except Exception as e:  # noqa: BLE001
        LOG.warning("events: token rifiutato: %s", e)
        return None, False
    # `principal` è chi agisce (una persona, un proxy); `agent` è il carrier che
    # ha firmato. Filtrare sul carrier darebbe a un proxy la visibilità di
    # clodia, che partecipa quasi ovunque.
    chi = str(payload.get("principal") or payload.get("agent") or "").strip()
    if not chi:
        return None, False
    spec = registry.get_by_name(chi)
    return chi, bool(spec is not None and getattr(spec, "type", "") == "proxy")


def _event_visible(chi: str, is_proxy: bool, ev) -> bool:
    """Un evento raggiunge chi ha diritto di vedere la stanza da cui viene.

    Gli eventi di canale portano `tier`, `name` **e il testo del messaggio**:
    finché questo stream era aperto e non filtrato, chiunque raggiungesse la
    porta leggeva le conversazioni di ogni topic, tier alti inclusi.

    Gli eventi senza stanza (attività di un agente, ciclo di vita di una chat)
    restano per chi opera nella webui, ma **non** per un proxy: un sistema terzo
    vede la stanza in cui è stato ammesso e nient'altro.
    """
    # Import qui e non in testa: `topics` importa da questo modulo, e la
    # dipendenza circolare romperebbe l'avvio.
    from .topics import _visible_to

    payload = getattr(ev, "payload", None) or {}
    tier = str(payload.get("tier") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not (tier and name):
        return not is_proxy
    meta = _room_meta(tier, name)
    if meta is None:
        return False
    return _visible_to(chi, {"owner": meta.get("owner"),
                             "participants": meta.get("participants") or []})


@router.get("/clodia/events")
async def events(request: Request):
    """SSE: gli eventi delle stanze che chi ascolta ha diritto di vedere.

    Era un broadcast globale senza autenticazione — nato aperto perché
    `EventSource` non manda header, e rimasto tale mentre gli eventi si
    arricchivano del testo dei messaggi.
    """
    chi, is_proxy = _stream_principal(request)
    if not chi:
        return JSONResponse(
            {"error": "questo stream richiede un token: header Authorization "
                      "oppure ?token= (EventSource non manda header)"},
            status_code=401)

    async def event_stream():
        async for ev in bus.subscribe():
            try:
                if not _event_visible(chi, is_proxy, ev):
                    continue
            except Exception:  # noqa: BLE001 — in dubbio non si consegna
                continue
            yield {"data": json.dumps(ev.model_dump(), default=str)}
    return EventSourceResponse(event_stream())
