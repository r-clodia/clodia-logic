"""Profilo dati personali (PII) per-agent — UI-facing.

L'agent-server NON tiene i PII: vivono nel vault del GATEWAY (segregazione). Qui
facciamo da proxy autenticato: ricaviamo il principal dall'utente connesso (ckt1),
coniamo un token PER QUEL principal e chiamiamo `/internal/profile/*` del gateway,
che applica l'ACL (self/admin/grant). Così "self gestisce i propri dati, l'admin
tutto", senza che i PII passino mai da un modello.
"""
from __future__ import annotations

import os

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..colony import pki
from .agents import _principal_from_request

router = APIRouter()
_HTTP_TIMEOUT = 15


def _base_url() -> str:
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/").rstrip("/")
    base = mcp[: -len("/mcp")] if mcp.endswith("/mcp") else mcp
    return f"{base}/internal/profile"


# Identità di servizio dell'agent-server verso il gateway: ha chiave server-side
# (i super-agent sono emessi da issue-all). Gli umani firmano client-side e NON
# hanno chiave qui → non possiamo coniare a loro nome. Coniamo come servizio e
# dichiariamo il principal reale via header; il gateway (che si fida del servizio)
# applica l'ACL su quel principal.
_SERVICE = os.environ.get("CLODIA_PROFILE_SERVICE", "clodia")


def _headers(principal: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {pki.mint_session_token(_SERVICE, ttl_seconds=300)}",
        "X-Clodia-Principal": principal,
    }


def _principal(request: Request) -> str:
    p = _principal_from_request(request)
    if not p:
        raise HTTPException(401, "non autenticato")
    return p


def _relay(resp: requests.Response):
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", resp.text)
        except Exception:
            detail = resp.text
        raise HTTPException(resp.status_code, detail[:200])
    return resp.json()


@router.get("/clodia/agents/{name}/profile")
def get_profile(name: str, request: Request):
    p = _principal(request)
    r = requests.get(f"{_base_url()}/{name}", headers=_headers(p), timeout=_HTTP_TIMEOUT)
    return _relay(r)


@router.put("/clodia/agents/{name}/profile")
async def put_profile(name: str, request: Request):
    p = _principal(request)
    body = await request.json()
    r = requests.put(f"{_base_url()}/{name}", json={"fields": body.get("fields", {})},
                     headers=_headers(p), timeout=_HTTP_TIMEOUT)
    return _relay(r)


@router.post("/clodia/agents/{name}/profile/grant")
async def grant_profile(name: str, request: Request):
    p = _principal(request)
    body = await request.json()
    r = requests.post(f"{_base_url()}/{name}/grant",
                      json={"grantee": body.get("grantee"), "granted": body.get("granted", True)},
                      headers=_headers(p), timeout=_HTTP_TIMEOUT)
    return _relay(r)


@router.get("/clodia/agents/{name}/profile/files")
def list_profile_files(name: str, request: Request):
    p = _principal(request)
    r = requests.get(f"{_base_url()}/{name}/files", headers=_headers(p), timeout=_HTTP_TIMEOUT)
    return _relay(r)


@router.post("/clodia/agents/{name}/profile/files")
async def upload_profile_file(name: str, request: Request):
    p = _principal(request)
    body = await request.json()
    r = requests.post(f"{_base_url()}/{name}/files", json=body, headers=_headers(p), timeout=60)
    return _relay(r)


@router.get("/clodia/agents/{name}/profile/files/{filename}")
def download_profile_file(name: str, filename: str, request: Request):
    p = _principal(request)
    r = requests.get(f"{_base_url()}/{name}/files/{filename}", headers=_headers(p), timeout=60)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, "download non riuscito")
    from urllib.parse import quote
    return Response(content=r.content, media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8\'\'{quote(filename)}"})


@router.delete("/clodia/agents/{name}/profile/files/{filename}")
def delete_profile_file(name: str, filename: str, request: Request):
    p = _principal(request)
    r = requests.delete(f"{_base_url()}/{name}/files/{filename}", headers=_headers(p), timeout=_HTTP_TIMEOUT)
    return _relay(r)
