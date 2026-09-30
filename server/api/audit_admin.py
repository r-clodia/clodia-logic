"""Admin access to the audit trail (clodia-platform#447, part 2).

The trail and the evidence live on the gateway. These routes are the human
door to them: platform admin only, the reader is the verified person, and the
gateway records every export and every evidence read under that name.

    GET  /api/admin/audit/status
    POST /api/admin/audit/export     {since?, until?, purpose?}  → signed bundle
    GET  /api/admin/audit/evidence?hash=&tier=
"""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from . import admin
from .agents import _principal_from_request

router = APIRouter(prefix="/api/admin/audit", tags=["audit"])


def _base() -> str:
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/")
    base = mcp.rstrip("/")
    return base[: -len("/mcp")] if base.endswith("/mcp") else base


def _headers() -> dict:
    return {"X-Orchestrator-Secret": (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip()}


def _require_admin(request: Request) -> str:
    who = _principal_from_request(request)
    if not who:
        raise HTTPException(401, "autenticazione richiesta")
    if not admin.is_admin(who):
        raise HTTPException(403, "il registro di audit è consultabile da un admin della piattaforma")
    return who


def _clearance_of(who: str) -> str:
    from ..agents import registry
    try:
        spec = registry.get_by_name(who)
    except Exception:  # noqa: BLE001
        spec = None
    return getattr(spec, "clearance", None) or "SEAL-0"


@router.get("/status")
async def status(request: Request):
    _require_admin(request)
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(_base() + "/internal/audit/status", headers=_headers())
    if r.status_code != 200:
        raise HTTPException(502, "gateway: stato del registro non disponibile")
    return r.json()


@router.post("/export")
async def export(request: Request):
    who = _require_admin(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    payload = {"reader": who, "since": body.get("since"), "until": body.get("until"),
               "purpose": body.get("purpose") or ""}
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(_base() + "/internal/audit/export", json=payload, headers=_headers())
    if r.status_code != 200:
        raise HTTPException(r.status_code if r.status_code < 500 else 502, r.text[:300])
    return Response(r.content, media_type="application/gzip", headers={
        "Content-Disposition": 'attachment; filename="clodia-audit-export.tgz"'})


@router.get("/evidence")
async def evidence(request: Request, hash: str, tier: str):  # noqa: A002 - query name
    who = _require_admin(request)
    params = {"hash": hash, "tier": tier, "reader": who, "clearance": _clearance_of(who)}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(_base() + "/internal/audit/evidence", params=params, headers=_headers())
    if r.status_code == 403:
        raise HTTPException(403, "la tua clearance non copre il livello di questa evidenza")
    if r.status_code != 200:
        raise HTTPException(404 if r.status_code == 404 else 502, "evidenza non disponibile")
    return Response(r.content, media_type="application/octet-stream")
