"""Lettura dell'inventario RAG dal gateway — per la pagina Databases.

Stesso modello di `provider_store.py`: il runner di clodia-logic (processo
trusted-core, non un modello) parla un endpoint interno del gateway
(`/internal/rag/collections`) autenticandosi con un token ckt1 firmato per il
principal privilegiato (default `clodia`). A differenza del verbo MCP
`rag.collections`, questa lettura non è filtrata per i grant di un agente: un
admin che gestisce l'inventario deve vedere tutte le collection, comprese
quelle orfane.

Endpoint del gateway (vedi clodia-tools `server/rag_api.py`):
  GET /internal/rag/collections   → {"collections": [...]}
"""
from __future__ import annotations

import asyncio
import logging
import os

import requests

from ..colony import pki

LOG = logging.getLogger("agent-server.api.rag_store")

_PRINCIPAL = os.environ.get("CLODIA_PROVIDER_PRINCIPAL", "clodia")
_TOKEN_TTL = 300
_HTTP_TIMEOUT = 10


class RagStoreError(RuntimeError):
    """Il gateway o il servizio eu-rag-search non sono raggiungibili."""


def _base_url() -> str:
    explicit = os.environ.get("CLODIA_TOOLS_RAG_URL")
    if explicit:
        return explicit.rstrip("/")
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/")
    base = mcp.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return f"{base}/internal/rag/collections"


def _headers() -> dict[str, str]:
    token = pki.mint_session_token(_PRINCIPAL, ttl_seconds=_TOKEN_TTL)
    return {"Authorization": f"Bearer {token}"}


def list_collections() -> list[dict]:
    """Tutte le collection RAG esistenti, non filtrate per grant.

    Ritorna lista vuota se il gateway segnala il servizio eu-rag-search
    irraggiungibile (502): un inventario incompleto per un guasto infra non è
    un errore da propagare all'intera pagina — le altre righe (i datastore)
    restano leggibili. Solleva `RagStoreError` solo se il GATEWAY stesso non
    risponde: quello sì impedisce di sapere qualunque cosa.
    """
    try:
        r = requests.get(_base_url(), headers=_headers(), timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise RagStoreError(f"gateway irraggiungibile per GET collections: {e}") from e
    if r.status_code == 502:
        LOG.warning("rag_store: eu-rag-search irraggiungibile, inventario RAG vuoto")
        return []
    if r.status_code != 200:
        raise RagStoreError(f"gateway GET collections → HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError as e:
        raise RagStoreError("gateway GET collections: risposta non JSON") from e
    coll = data.get("collections") if isinstance(data, dict) else None
    return coll if isinstance(coll, list) else []


async def list_collections_async() -> list[dict]:
    """`list_collections` per gli handler `async def` (#106): la GET è
    bloccante, chiamarla dritta da un async fermerebbe l'event loop di tutto
    il processo, non solo questa richiesta."""
    return await asyncio.to_thread(list_collections)
