"""Proxy owner→gateway per l'escalation SUDO (M-sudo, flusso richiesta/approva).

La webUI (owner) vede le richieste pending e approva/nega; qui inoltriamo al
gateway inserendo il PRINCIPAL umano verificato della sessione (l'approvatore).
Il gateway è la fonte autoritativa: gata su `sudo.is_approver(principal)`, quindi
un non-approvatore (es. giovanni) viene comunque negato lì.
"""
from __future__ import annotations

import logging
import os

import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..colony import pki
from .agents import _principal_from_request

LOG = logging.getLogger("agent-server.api.sudo")
router = APIRouter()

_TOKEN_TTL = 120
_HTTP_TIMEOUT = 15


def _gw_base() -> str:
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/")
    base = mcp.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return f"{base}/internal/sudo"


def _gw(method: str, path: str, principal: str, json: dict | None = None):
    """Chiama il gateway inoltrando il PRINCIPAL umano (approvatore) nel token."""
    token = pki.mint_session_token("clodia", ttl_seconds=_TOKEN_TTL, principal=principal)
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{_gw_base()}{path}"
    r = requests.request(method, url, headers=headers, json=json, timeout=_HTTP_TIMEOUT)
    return r


@router.get("/api/sudo/pending")
async def pending(request: Request):
    """Richieste di escalation in attesa (per il popup dell'owner)."""
    principal = _principal_from_request(request)
    if not principal:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    r = _gw("GET", "/pending", principal)
    if r.status_code == 403:
        # non-approvatore: nessuna richiesta da mostrargli (non è un errore per la UI)
        return JSONResponse({"requests": []})
    return JSONResponse(r.json(), status_code=r.status_code)


@router.post("/api/sudo/approve")
async def approve(request: Request):
    """Owner approva: concede sudo a (agent, instance) per N minuti."""
    principal = _principal_from_request(request)
    if not principal:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    payload = {"agent": (body.get("agent") or "").strip(),
               "instance": (body.get("instance") or "-").strip() or "-",
               "minutes": body.get("minutes", 15)}
    r = _gw("POST", "/grant", principal, payload)
    LOG.info("sudo approve %s@%s da %s → %s", payload["agent"], payload["instance"],
             principal, r.status_code)
    return JSONResponse(r.json(), status_code=r.status_code)


@router.post("/api/sudo/deny")
async def deny(request: Request):
    """Owner nega la richiesta pending."""
    principal = _principal_from_request(request)
    if not principal:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    payload = {"agent": (body.get("agent") or "").strip(),
               "instance": (body.get("instance") or "-").strip() or "-"}
    r = _gw("POST", "/deny", principal, payload)
    return JSONResponse(r.json(), status_code=r.status_code)
