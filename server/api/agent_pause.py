"""API REST per il kill-switch agent.

POST /api/agents/{name}/pause  → tutte le istanze running cancellate,
                                  niente nuovi claim finché non resume
POST /api/agents/{name}/resume → riprende i claim normali
GET  /api/agents/paused        → lista degli agent in pausa
"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException

from ..agents import pause as pause_mod
from ..agents.loader import registry

router = APIRouter(prefix="/api/agents", tags=["agents-pause"])


@router.get("/_paused")
async def list_paused() -> dict:
    return {"paused": pause_mod.list_paused()}


@router.post("/{name}/pause")
async def pause_agent(name: str) -> dict:
    if registry.get_by_name(name) is None:
        raise HTTPException(404, f"agent '{name}' non registrato")
    return pause_mod.pause(name)


@router.post("/{name}/resume")
async def resume_agent(name: str) -> dict:
    if registry.get_by_name(name) is None:
        raise HTTPException(404, f"agent '{name}' non registrato")
    return pause_mod.resume(name)
