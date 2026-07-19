"""Client verso il **PDP unico** (gateway) per le azioni di piattaforma umane.

M-authz: la webui non autorizza più da sé. Ogni endpoint REST privilegiato o
INOLTRA l'azione al tool gateway (`gw_tool`, esecuzione al gateway) o CHIEDE al
gateway se è consentita (`gw_authorize`) e poi esegue localmente. La decisione è
sempre e solo del gateway, con la stessa RBAC degli agenti — un solo meccanismo
valido per agenti e umani.

Il ruolo umano (admin|user) è determinato qui da `admin.is_admin` e messo come
claim FIRMATO nel token (l'agent-server è trusted): il gateway lo verifica ma non
si fida di un header arbitrario.
"""
from __future__ import annotations

import logging
import os

import requests
from fastapi import HTTPException, Request

from ..colony import pki
from . import admin
from .agents import _principal_from_request

LOG = logging.getLogger("agent-server.api.gateway_pdp")

_TOKEN_TTL = 120
_HTTP_TIMEOUT = 30
_CARRIER = "clodia"  # agent-carrier che firma il token on-behalf (identità trusted)


def _gw_base() -> str:
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/")
    base = mcp.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return base


def human_role(principal: str | None) -> str:
    """Ruolo dell'umano per la RBAC: admin (superadmin/admin) o user."""
    return "admin" if admin.is_admin(principal) else "user"


def _token(principal: str) -> str:
    return pki.mint_session_token(
        _CARRIER, ttl_seconds=_TOKEN_TTL, principal=principal,
        on_behalf=True, human_role=human_role(principal))


def gw_authorize(tool: str, principal: str) -> bool:
    """True se l'umano `principal` può invocare `tool` (decisione del gateway)."""
    r = requests.post(f"{_gw_base()}/internal/authorize",
                      headers={"Authorization": f"Bearer {_token(principal)}"},
                      json={"tool": tool}, timeout=_HTTP_TIMEOUT)
    if r.status_code != 200:
        LOG.warning("authorize '%s' per '%s' → HTTP %s", tool, principal, r.status_code)
        return False
    return bool(r.json().get("allowed"))


def gw_tool(tool: str, arguments: dict, principal: str) -> tuple[int, dict]:
    """Inoltra l'esecuzione del tool al gateway (PDP + esecuzione). Ritorna
    (status_http, json)."""
    r = requests.post(f"{_gw_base()}/internal/tool",
                      headers={"Authorization": f"Bearer {_token(principal)}"},
                      json={"tool": tool, "arguments": arguments}, timeout=_HTTP_TIMEOUT)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"error": "bad_gateway_response"}


# ── Guard per gli endpoint FastAPI ───────────────────────────────────────────
def require_authz(request: Request, tool: str) -> str:
    """Guard per le azioni ESEGUITE localmente dall'agent-server: verifica il
    principal umano e chiede al gateway se `tool` è consentito. Solleva 401 se
    anonimo, 403 se negato. Ritorna il principal. Fail-CLOSED su gateway irraggiungibile."""
    principal = _principal_from_request(request)
    if not principal:
        raise HTTPException(401, "autenticazione richiesta")
    if not gw_authorize(tool, principal):
        raise HTTPException(403, f"azione '{tool}' riservata agli admin")
    return principal


def forward(request: Request, tool: str, arguments: dict):
    """Guard+INOLTRO per le azioni implementate come tool gateway: verifica il
    principal, poi delega esecuzione+decisione al gateway. Ritorna il `result`
    del tool o solleva l'HTTPException corrispondente (403/400)."""
    principal = _principal_from_request(request)
    if not principal:
        raise HTTPException(401, "autenticazione richiesta")
    status, data = gw_tool(tool, arguments, principal)
    if status == 200:
        return data.get("result")
    if status == 403:
        raise HTTPException(403, data.get("detail") or "azione non consentita")
    raise HTTPException(status if status >= 400 else 502,
                        data.get("detail") or data.get("error") or "errore gateway")
