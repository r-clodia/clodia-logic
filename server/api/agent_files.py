"""Proxy file localhost-only PER IL BASH DELL'AGENT.

Permette al bash dell'agent (che gira DENTRO il container agent-server) di
scaricare/caricare file binari di un topic **senza farli transitare in base64
dentro il modello** — causa dei troncamenti su file grandi (es. un template
xlsx da ~97KB → ~130KB di base64 che il modello non riesce a riemettere intero).
Qui i byte li muove l'agent-server in Python; il modello scrive solo un comando
curl corto con un path locale.

Sicurezza: SOLO localhost (127.0.0.1). Solo il bash dell'agent nello stesso
container raggiunge 127.0.0.1:7842; gli altri container passano dalla rete
docker (172.x) e sono bloccati. NB: usa le credenziali interne del backend
(accesso pieno ai topic) — lo scoping ACL per-agent è un follow-up, oggi serve
agli agent super (clodia/ophelia).
"""
from __future__ import annotations

import base64
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from . import topics_client

router = APIRouter()

_SCRATCH = Path("/tmp/clodia-agent-files")
_ALLOWED_LOCAL_ROOTS = ("/tmp/",)


def _require_localhost(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "endpoint riservato al bash locale dell'agent")


def _safe_local(path: str) -> Path:
    p = Path(path).resolve()
    if not any(str(p).startswith(r) for r in _ALLOWED_LOCAL_ROOTS):
        raise HTTPException(400, "local_path deve stare sotto /tmp/")
    return p


@router.get("/clodia/agent/fetch-file")
async def fetch_file(request: Request, tier: str, name: str, path: str) -> dict:
    """Scarica un file del topic in uno scratch locale e ritorna `local_path`.
    Il bash dell'agent ci lavora con python/openpyxl: i byte non entrano mai nel
    contesto del modello."""
    _require_localhost(request)
    try:
        data = topics_client.read_file(tier, name, path)
    except topics_client.TopicsClientError as e:
        raise HTTPException(404, str(e))
    _SCRATCH.mkdir(parents=True, exist_ok=True)
    dest = _SCRATCH / f"{uuid.uuid4().hex}{Path(path).suffix}"
    dest.write_bytes(data)
    return {"local_path": str(dest), "size": len(data)}


@router.post("/clodia/agent/put-file")
async def put_file(request: Request) -> dict:
    """Carica nel topic un file preparato in scratch locale (`local_path`): legge
    i byte qui in Python e li manda al gateway in base64 — il modello non li
    tocca, quindi niente troncamento."""
    _require_localhost(request)
    body = await request.json()
    tier = body.get("tier")
    name = body.get("name")
    filename = body.get("filename")
    local_path = body.get("local_path")
    if not all([tier, name, filename, local_path]):
        raise HTTPException(400, "tier, name, filename, local_path richiesti")
    p = _safe_local(local_path)
    if not p.is_file():
        raise HTTPException(404, f"local_path non trovato: {local_path}")
    content_b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    try:
        res = topics_client.put_file(tier, name, filename, content_b64)
    except topics_client.TopicsClientError as e:
        raise HTTPException(502, str(e))
    out = {"ok": True, "filename": filename, "size": p.stat().st_size}
    if isinstance(res, dict):
        out.update(res)
    return out
