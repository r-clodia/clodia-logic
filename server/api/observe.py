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


def _gw_post(path: str, payload: dict | None = None):
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/").rstrip("/")
    base = mcp[: -len("/mcp")] if mcp.endswith("/mcp") else mcp
    secret = (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip()
    return requests.post(f"{base}{path}", json=payload or {},
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


@router.post("/api/observe/whitelist/{direction}/{action}")
async def whitelist_edit(direction: str, action: str, request: Request):
    """Aggiunge o rimuove una voce dalle liste, a mano.

    Richiesta dell'owner, 17 ago 2026: «devo poter inserire un egress o ingress
    anche a mano». Il dialog del gate resta la via giusta quando la destinazione
    la chiede un agente — l'informazione lì è completa («@clodia vuole scrivere a
    X») — ma non è una via quando l'owner sa già cosa censire: le cento fonti di
    un digest non passano da cento dialog.

    ADMIN-ONLY, ed è qui che si decide: il gateway esegue e non conosce i ruoli
    umani. Concedere una destinazione è più privilegiato del singolo invio, perché
    la rende silenziosa per sempre; togliere una fonte fidata rimette in funzione
    un segnale. Nessuna delle due è un'operazione da partecipante.

    La validazione la fa il gateway (`egress.check_grantable`): schemi della
    direzione sbagliata e voci degeneri sono rifiutati là, e il messaggio torna
    così com'è. Duplicarla qui vorrebbe dire due regole che divergono.
    """
    from . import admin
    principal = _principal(request)
    if not principal:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not admin.is_admin(principal):
        return JSONResponse({"error": "solo un admin può modificare le liste"},
                            status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    uri = str((body or {}).get("uri") or "").strip()
    if not uri:
        return JSONResponse({"error": "uri richiesto"}, status_code=400)
    try:
        r = _gw_post(f"/internal/egress/whitelist/{direction}/{action}", {"uri": uri})
    except Exception as e:  # noqa: BLE001
        LOG.warning("whitelist %s/%s: gateway irraggiungibile (%s)",
                    direction, action, str(e)[:120])
        return JSONResponse({"error": "gateway irraggiungibile"}, status_code=503)
    # Il 400 del gateway porta il MOTIVO del rifiuto: si inoltra invece di
    # tradurlo in «non valido», che non dice come correggere.
    return JSONResponse(r.json() if r.content else {"ok": r.status_code == 200},
                        status_code=r.status_code)


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
