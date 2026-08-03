"""Ponte verso le superfici di osservazione del gateway (clodia-platform#104).

Perché un ponte e non una lettura diretta: il registro dei verbi e la whitelist
delle destinazioni vivono sul volume del **solo** gateway, che l'agent-server non
monta di proposito — chi può riscrivere i propri limiti non ha limiti (#80). La
webui parla con l'agent-server, quindi qui c'è l'unico passaggio possibile.

Entrambe le rotte sono di sola lettura e riservate a un umano autenticato: sono i
dati con cui l'owner decide quali controlli attivare, non dati operativi per un
agente.
"""
from __future__ import annotations

import logging
import os

import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

LOG = logging.getLogger("agent-server.api.observe")
router = APIRouter()

_HTTP_TIMEOUT = 6.0


def _gw(path: str, params: dict | None = None):
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/").rstrip("/")
    base = mcp[: -len("/mcp")] if mcp.endswith("/mcp") else mcp
    secret = (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip()
    return requests.get(f"{base}{path}", params=params or {},
                        headers={"X-Orchestrator-Secret": secret},
                        timeout=_HTTP_TIMEOUT)


def _principal(request: Request) -> str | None:
    from .gate import _principal_from_request
    return _principal_from_request(request)


@router.get("/api/observe/recent")
async def recent(request: Request, since: int = 0):
    """Gate che sarebbero scattati dopo `since` (epoch). Alimenta il footer.

    Best-effort: se il gateway non risponde si ritorna una lista vuota invece di
    un errore. Un feedback che rompe la pagina è peggio di un feedback assente —
    e la pagina serve a lavorare, non a guardare le osservazioni.
    """
    if not _principal(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        r = _gw("/internal/observations", {"since": int(since or 0)})
        r.raise_for_status()
        return JSONResponse(r.json())
    except Exception as e:  # noqa: BLE001
        LOG.warning("observe: osservazioni non leggibili (%s)", str(e)[:120])
        return JSONResponse({"observing": False, "observations": []})


@router.get("/api/observe/whitelist")
async def whitelist(request: Request):
    """Whitelist delle destinazioni per agente e per tipo (sola lettura)."""
    if not _principal(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        r = _gw("/internal/egress/whitelist")
        r.raise_for_status()
        return JSONResponse(r.json())
    except Exception as e:  # noqa: BLE001
        LOG.warning("observe: whitelist non leggibile (%s)", str(e)[:120])
        return JSONResponse({"mode": "unknown", "agents": {}, "types": []})
