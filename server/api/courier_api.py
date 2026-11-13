"""The courier's internal door (owner, 14 Nov 2026).

Reading mail is the courier's alone — deterministic code, never an agent. An
agent may only ask it to check the room NOW (`topic.mail_check` on the
gateway), which calls this route with the orchestrator secret.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .transfers import _authorize

router = APIRouter(tags=["internal-courier"])


@router.post("/internal/courier/check")
async def courier_check(request: Request) -> dict:
    """An agent asked the courier to check the room's mail NOW
    (`topic.mail_check`, owner 14 Nov 2026): the gateway calls this with the
    orchestrator secret; reading mail stays the courier's — deterministic code,
    never an agent."""
    _authorize(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    tier, name = str((body or {}).get("tier") or ""), str((body or {}).get("name") or "")
    if not (tier and name):
        raise HTTPException(400, "tier e name richiesti")
    from . import mail_courier
    n = await mail_courier.run_cycle(only=(tier, name))
    return {"delivered": n}
