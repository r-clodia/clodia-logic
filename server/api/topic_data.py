"""Topic-scoped data, the webui's door (clodia-platform#590).

The gateway holds the catalog, the imports and the decision (clodia-tools
`data_catalog`, `topic_data`, `data_migration`). Here: WHO may ask.

- `GET  /clodia/data-catalog`: any person; a non-admin sees the resources
  they may import.
- `POST /clodia/data-catalog`: admins (update tier/importers, retire, sync,
  migration plan/apply, the `data_access` switch).
- `POST /clodia/channels/{tier}/{name}/data`: the topic's owner, a PERSON
  (import, remove, grant). The gateway re-checks importer, clearance, tier.
- `GET|POST /clodia/channels/{tier}/{name}/tools` (#609): the room's toolbox —
  read by its participants, changed by its owner (or an admin), a person.
- `GET|POST /clodia/dm-tools` (#609): the verbs enabled outside a room — read
  by any person, changed by an admin.

The person is forwarded as `by` with role and clearance: the gateway trusts
those fields only on the agent-server's signed door.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from . import admin, topics_client

LOG = logging.getLogger("agent-server.topic_data")
router = APIRouter()

_ADMIN_ACTIONS = {"update", "retire", "sync", "migration_plan", "migration_apply", "set_mode"}


def _person(request: Request) -> str:
    from .topics import _principal_from_request
    who = _principal_from_request(request)
    if not who:
        raise HTTPException(401, "autenticazione richiesta")
    if not admin.is_human(who):
        raise HTTPException(403, "i dati di un topic li governa una persona, non un agente")
    return who


def _actor(who: str) -> dict:
    from .topics import _principal_clearance
    return {"by": who, "by_admin": bool(admin.is_admin(who)),
            "by_clearance": _principal_clearance(who)}


def _gw(e: topics_client.TopicsClientError) -> HTTPException:
    if getattr(e, "is_client_error", False):
        return HTTPException(e.status, e.detail)
    return HTTPException(502, str(e)[:200])


@router.get("/clodia/data-catalog")
async def get_catalog(request: Request) -> dict:
    who = _person(request)
    try:
        out = await asyncio.to_thread(topics_client.data_catalog)
    except topics_client.TopicsClientError as e:
        raise _gw(e)
    if not admin.is_admin(who):
        # What they may import, without the topics that import it (topic names
        # of rooms they may not be in).
        out["entries"] = [{k: v for k, v in e.items() if k not in ("importing_topics", "topic")}
                          for e in out.get("entries") or []
                          if who in (e.get("importers") or [])]
    return out


@router.post("/clodia/data-catalog")
async def post_catalog(request: Request) -> dict:
    who = _person(request)
    if not admin.is_admin(who):
        raise HTTPException(403, "il catalogo dei dati lo amministra un admin")
    body = await request.json()
    if (body or {}).get("action") not in _ADMIN_ACTIONS:
        raise HTTPException(400, "azione sconosciuta")
    try:
        return await asyncio.to_thread(topics_client.data_catalog, {**body, **_actor(who)})
    except topics_client.TopicsClientError as e:
        raise _gw(e)


@router.post("/clodia/channels/{tier}/{name}/data")
async def channel_data(tier: str, name: str, request: Request) -> dict:
    from .channels import _require_scope_owner
    topic = await topics_client.async_open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "canale non trovato")
    who = _person(request)
    if not admin.is_admin(who):
        # The owner, or a platform admin (a topic owned by an agent has no
        # person to own it otherwise; the gateway admits admins too).
        who = _require_scope_owner(request, topic.get("meta", {}))
    if not admin.is_human(who):
        raise HTTPException(403, "i dati di un topic li governa una persona, non un agente")
    body = await request.json()
    if (body or {}).get("action") not in ("import", "remove", "grant"):
        raise HTTPException(400, "azione: import | remove | grant")
    try:
        return await asyncio.to_thread(topics_client.topic_data_action, tier, name,
                                       {**body, **_actor(who)})
    except topics_client.TopicsClientError as e:
        raise _gw(e)


@router.post("/clodia/channels/{tier}/{name}/index")
async def channel_index(tier: str, name: str, request: Request) -> dict:
    """The topic's own RAG index on/off (#599): the owner (or an admin), a person.
    Off drops the collection's documents."""
    from .channels import _require_scope_owner
    topic = await topics_client.async_open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "canale non trovato")
    who = _person(request)
    if not admin.is_admin(who):
        who = _require_scope_owner(request, topic.get("meta", {}))
    if not admin.is_human(who):
        raise HTTPException(403, "l'indice di un topic lo governa una persona, non un agente")
    body = await request.json()
    enabled = (body or {}).get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(400, "enabled: true | false")
    try:
        return await asyncio.to_thread(topics_client.topic_index_action, tier, name,
                                       {"action": "enable" if enabled else "disable",
                                        **_actor(who)})
    except topics_client.TopicsClientError as e:
        raise _gw(e)


async def _room_person(request: Request, tier: str, name: str, *, owner: bool) -> str:
    """A person of the room: its owner (or an admin) to change, a participant
    to read. Agents never pass: the toolbox is a wall of the room."""
    from .channels import _require_member, _require_scope_owner
    # Who asks, before the topic is opened: an anonymous caller must not learn
    # whether a topic exists (401 vs 404).
    who = _person(request)
    topic = await topics_client.async_open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "canale non trovato")
    if admin.is_admin(who):
        return who
    meta = topic.get("meta", {})
    return _require_scope_owner(request, meta) if owner else _require_member(request, meta)


#: The actions of the room's tools (#609), and the MCP form of #620: whole
#: MCPs, their resource rows and the per-agent levels. The gateway validates
#: every argument; this list only keeps unknown actions off its door.
_ROOM_TOOL_ACTIONS = ("enable", "disable", "except", "unexcept",
                      "enable_mcp", "disable_mcp", "set_resource", "unset_resource",
                      "except_mcp")


@router.get("/clodia/channels/{tier}/{name}/tools")
async def get_channel_tools(tier: str, name: str, request: Request) -> dict:
    await _room_person(request, tier, name, owner=False)
    try:
        return await asyncio.to_thread(topics_client.topic_tools, tier, name)
    except topics_client.TopicsClientError as e:
        raise _gw(e)


@router.post("/clodia/channels/{tier}/{name}/tools")
async def post_channel_tools(tier: str, name: str, request: Request) -> dict:
    who = await _room_person(request, tier, name, owner=True)
    body = await request.json()
    if (body or {}).get("action") not in _ROOM_TOOL_ACTIONS:
        raise HTTPException(400, "azione: " + " | ".join(_ROOM_TOOL_ACTIONS))
    try:
        return await asyncio.to_thread(topics_client.topic_tools, tier, name,
                                       {**body, **_actor(who)})
    except topics_client.TopicsClientError as e:
        raise _gw(e)


@router.get("/clodia/dm-tools")
async def get_dm_tools(request: Request) -> dict:
    _person(request)
    try:
        return await asyncio.to_thread(topics_client.dm_tools)
    except topics_client.TopicsClientError as e:
        raise _gw(e)


@router.post("/clodia/dm-tools")
async def post_dm_tools(request: Request) -> dict:
    who = _person(request)
    if not admin.is_admin(who):
        raise HTTPException(403, "i verbi fuori dai canali li decide un admin")
    body = await request.json()
    if (body or {}).get("action") not in ("enable", "disable"):
        raise HTTPException(400, "azione: enable | disable")
    try:
        return await asyncio.to_thread(topics_client.dm_tools, {**body, **_actor(who)})
    except topics_client.TopicsClientError as e:
        raise _gw(e)
