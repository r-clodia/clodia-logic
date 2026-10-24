"""The room's toolbox, the webui's door (clodia-platform#609 R5, epic #604).

The gateway holds the toolbox, the catalogue of what is enablable and the
decision (clodia-tools `room_tools`). Here, as in `topic_data`: WHO may ask.

- `GET/POST /clodia/channels/{tier}/{name}/tools`: the topic's OWNER (or a
  platform admin), a person. Enabling a verb for every participant of a room
  is a wall of that room, and an agent does not move a wall.
- `GET  /clodia/dm-tools`: any person — the panel that shows it is the
  instance's settings.
- `POST /clodia/dm-tools`: a platform ADMIN. The verbs enabled outside a room
  are an instance setting: no topic owns them, so there is no owner to ask.

**The admin check lives here, before the call.** On the agent-server's signed
door the gateway admits the write without re-deriving the role
(`_authorize_topic_owner`, `dm_tools_route`): it trusts this side to have
checked, exactly as `data_catalog` does. A `403` that this module forgets is a
`403` nobody else performs.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from . import admin, topics_client

LOG = logging.getLogger("agent-server.topic_tools")
router = APIRouter()

#: What the gateway's `topic_tools_route` accepts. Checked here so a typo
#: answers 400 without a round trip.
_ROOM_ACTIONS = ("enable", "disable", "except", "unexcept")


def _person(request: Request, what: str) -> str:
    from .topics import _principal_from_request
    who = _principal_from_request(request)
    if not who:
        raise HTTPException(401, "autenticazione richiesta")
    if not admin.is_human(who):
        raise HTTPException(403, f"{what} lo governa una persona, non un agente")
    return who


def _actor(who: str) -> dict:
    return {"by": who, "by_admin": bool(admin.is_admin(who))}


def _gw(e: topics_client.TopicsClientError) -> HTTPException:
    if getattr(e, "is_client_error", False):
        return HTTPException(e.status, e.detail)
    return HTTPException(502, str(e)[:200])


async def _owner_of_the_room(tier: str, name: str, request: Request) -> str:
    from .channels import _require_scope_owner
    topic = await topics_client.async_open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "canale non trovato")
    who = _person(request, "gli strumenti di un canale")
    if not admin.is_admin(who):
        # The owner, or a platform admin (a topic owned by an agent has no
        # person to own it otherwise; the gateway admits admins too).
        who = _require_scope_owner(request, topic.get("meta", {}))
    if not admin.is_human(who):
        raise HTTPException(403, "gli strumenti di un canale li governa una persona, "
                                 "non un agente")
    return who


@router.get("/clodia/channels/{tier}/{name}/tools")
async def get_channel_tools(tier: str, name: str, request: Request) -> dict:
    """`{enabled, except, enablable, gated}` for the room's panel."""
    await _owner_of_the_room(tier, name, request)
    try:
        return await asyncio.to_thread(topics_client.topic_tools, tier, name)
    except topics_client.TopicsClientError as e:
        raise _gw(e)


@router.post("/clodia/channels/{tier}/{name}/tools")
async def post_channel_tools(tier: str, name: str, request: Request) -> dict:
    """enable | disable | except | unexcept. The gateway re-checks the verb
    against what a room may enable, and answers 400 saying why."""
    who = await _owner_of_the_room(tier, name, request)
    body = await request.json()
    if (body or {}).get("action") not in _ROOM_ACTIONS:
        raise HTTPException(400, "azione: " + " | ".join(_ROOM_ACTIONS))
    try:
        return await asyncio.to_thread(topics_client.topic_tools, tier, name,
                                       {**body, **_actor(who)})
    except topics_client.TopicsClientError as e:
        raise _gw(e)


@router.get("/clodia/dm-tools")
async def get_dm_tools(request: Request) -> dict:
    """`{enabled, enablable, gated}`: the verbs enabled outside a room."""
    _person(request, "i verbi fuori da una stanza")
    try:
        return await asyncio.to_thread(topics_client.dm_tools)
    except topics_client.TopicsClientError as e:
        raise _gw(e)


@router.post("/clodia/dm-tools")
async def post_dm_tools(request: Request) -> dict:
    """enable | disable, an ADMIN only: no room owns this list."""
    who = _person(request, "i verbi fuori da una stanza")
    if not admin.is_admin(who):
        raise HTTPException(403, "i verbi fuori da una stanza li amministra un admin")
    body = await request.json()
    if (body or {}).get("action") not in ("enable", "disable"):
        raise HTTPException(400, "azione: enable | disable")
    try:
        return await asyncio.to_thread(topics_client.dm_tools, {**body, **_actor(who)})
    except topics_client.TopicsClientError as e:
        raise _gw(e)
