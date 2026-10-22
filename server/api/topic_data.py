"""Topic-scoped data, the webui's door (clodia-platform#590).

The gateway holds the catalog, the imports and the decision (clodia-tools
`data_catalog`, `topic_data`, `data_migration`). Here: WHO may ask.

- `GET  /clodia/data-catalog`: any person; a non-admin sees the resources
  they may import.
- `POST /clodia/data-catalog`: admins (update tier/importers, retire, sync,
  migration plan/apply, the `data_access` switch).
- `POST /clodia/channels/{tier}/{name}/data`: the topic's owner, a PERSON
  (import, remove, grant). The gateway re-checks importer, clearance, tier.

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
