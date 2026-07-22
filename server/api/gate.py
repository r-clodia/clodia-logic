"""Proxy umano→gateway per l'approvazione dei GATE (M-gate, sostituisce sudo).

La webUI/PWA mostra le richieste di gate pending; l'utente loggato **nel
contesto** approva/nega. Un gate si approva solo se l'approvatore è
**autorizzato al verbo dalla sua RBAC** (non puoi delegare ciò che non hai):
oggi la RBAC umana è binaria (admin), quindi i verbi gated li approva un admin.
L'approvazione conia una capability `ccap1` (cap=gate:<verb>) — prova
crittografica del consenso — e la registra nel gateway.
"""
from __future__ import annotations

import logging
import os

import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..colony import pki
from . import admin, topics_client
from .agents import _principal_from_request

LOG = logging.getLogger("agent-server.api.gate")
router = APIRouter()

_TOKEN_TTL = 120
_HTTP_TIMEOUT = 15


def _post_outcome(chat: str | None, principal: str, text: str) -> None:
    if not chat or not chat.startswith("chan:"):
        return
    parts = chat.split(":")
    if len(parts) < 3:
        return
    try:
        topics_client.post_message(parts[1], parts[2], principal, text, kind="human")
    except Exception as e:  # noqa: BLE001
        LOG.warning("post esito gate in chat %s fallito: %s", chat, e)


def _gw_base() -> str:
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/")
    base = mcp.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return f"{base}/internal/gate"


def _gw(method: str, path: str, principal: str, json: dict | None = None):
    token = pki.mint_session_token("clodia", ttl_seconds=_TOKEN_TTL, principal=principal)
    r = requests.request(method, f"{_gw_base()}{path}",
                         headers={"Authorization": f"Bearer {token}"},
                         json=json, timeout=_HTTP_TIMEOUT)
    return r


def _can_approve(principal: str, verb: str) -> bool:
    """L'approvatore può consentire `verb` solo se la SUA RBAC lo autorizza.
    RBAC umana oggi binaria: i verbi gated (≈ super-only) → admin. Owner/admin =
    qualunque verbo; utente semplice = nessun verbo gated (per ora)."""
    return admin.is_admin(principal)


@router.get("/api/gate/pending")
async def pending(request: Request):
    principal = _principal_from_request(request)
    if not principal:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    r = _gw("GET", "/pending", principal)
    if r.status_code >= 400:
        return JSONResponse({"requests": []})
    return JSONResponse(r.json(), status_code=r.status_code)


@router.post("/api/gate/approve")
async def approve(request: Request):
    """Approva un gate: consente a (agent, instance) l'uso di `verb`."""
    principal = _principal_from_request(request)
    if not principal:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    agent = (body.get("agent") or "").strip()
    instance = (body.get("instance") or "-").strip() or "-"
    verb = (body.get("verb") or "").strip()
    minutes = body.get("minutes", 10)
    if not (agent and verb):
        return JSONResponse({"error": "agent/verb richiesti"}, status_code=400)
    if not _can_approve(principal, verb):
        return JSONResponse(
            {"error": "forbidden",
             "detail": f"'{principal}' non è autorizzato al verbo '{verb}' (non puoi delegarlo)"},
            status_code=403)
    try:
        cap = pki.mint_capability(agent, instance, minutes, by=principal,
                                  cap=f"gate:{verb}")
    except Exception as e:  # noqa: BLE001
        LOG.error("mint_capability(gate) fallito per %s@%s:%s: %s", agent, instance, verb, e)
        return JSONResponse({"error": "mint_failed", "detail": str(e)}, status_code=500)
    r = _gw("POST", "/grant", principal,
            {"agent": agent, "instance": instance, "verb": verb, "token": cap["token"]})
    LOG.info("gate approve %s@%s:%s da %s (jti=%s) → %s", agent, instance, verb,
             principal, cap.get("jti"), r.status_code)
    if r.status_code == 200:
        _post_outcome(body.get("chat"), principal, f"🔓 gate approvato per @{agent}: {verb}")
    return JSONResponse(r.json(), status_code=r.status_code)


@router.post("/api/gate/deny")
async def deny(request: Request):
    principal = _principal_from_request(request)
    if not principal:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    agent = (body.get("agent") or "").strip()
    verb = (body.get("verb") or "").strip()
    r = _gw("POST", "/deny", principal,
            {"agent": agent, "instance": (body.get("instance") or "-").strip() or "-", "verb": verb})
    if r.status_code == 200:
        _post_outcome(body.get("chat"), principal, f"⛔ gate negato per @{agent}: {verb}")
    return JSONResponse(r.json(), status_code=r.status_code)
