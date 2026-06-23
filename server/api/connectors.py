"""Endpoint connettori delegabili (Fase 2) — admin only.

Proxy verso il gateway: lista dei connettori (account email) con lo stato di
grant per un agent, e toggle del grant. Riservato agli admin.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from . import admin, connectors_client
from .agents import _principal_from_request

router = APIRouter()


def _require_admin(request: Request) -> None:
    if not admin.is_admin(_principal_from_request(request)):
        raise HTTPException(403, "operazione riservata agli admin")


@router.get("/api/connectors")
async def list_connectors(request: Request, agent: str = "") -> dict:
    """Connettori (account email) con lo stato di grant per `agent`."""
    _require_admin(request)
    try:
        return {"connectors": connectors_client.list_connectors(agent or None)}
    except connectors_client.ConnectorsClientError as e:
        raise HTTPException(502, f"connettori non disponibili: {str(e)[:160]}")


@router.post("/api/connectors/grant")
async def grant_connector(request: Request) -> dict:
    """Abilita/disabilita un agent su un connettore (account email)."""
    _require_admin(request)
    body = await request.json()
    agent = (body.get("agent") or "").strip()
    account = (body.get("account") or "").strip()
    if not agent or not account:
        raise HTTPException(400, "agent e account richiesti")
    try:
        return connectors_client.grant(agent, account, bool(body.get("granted")))
    except connectors_client.ConnectorsClientError as e:
        raise HTTPException(502, f"grant fallito: {str(e)[:160]}")
