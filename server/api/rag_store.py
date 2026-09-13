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


def _rag_url(risorsa: str = "collections") -> str:
    """L'URL di una rotta `/internal/rag/*` del gateway.

    `CLODIA_TOOLS_RAG_URL` ha sempre indicato l'endpoint delle COLLECTION (era
    l'unico): resta quello il suo contratto, e da lì si ricava la base per le
    altre risorse. Reinterpretarlo come «base» romperebbe le istanze che lo
    configurano già.
    """
    explicit = (os.environ.get("CLODIA_TOOLS_RAG_URL") or "").rstrip("/")
    if explicit:
        base = (explicit[: -len("/collections")]
                if explicit.endswith("/collections") else explicit)
        return f"{base}/{risorsa}"
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/")
    base = mcp.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return f"{base}/internal/rag/{risorsa}"


def _base_url() -> str:
    """L'endpoint delle collection. Resta come nome perché è ciò che i
    chiamanti storici cercano."""
    return _rag_url("collections")


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


def list_documents(collection: str) -> list[dict]:
    """I documenti iniettati in UNA collection (nome, versione, status, chunk).

    A differenza di `list_collections`, un 502 qui NON degrada a lista vuota:
    l'inventario incompleto lascia il resto della pagina leggibile, mentre
    «questa collection non ha documenti» detto a chi li ha chiesti è una
    risposta diversa da «non ho potuto sapere», ed è falsa. Il chiamante la
    traduce in 503.
    """
    try:
        r = requests.get(_rag_url("documents"), headers=_headers(),
                         params={"collection": collection}, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise RagStoreError(f"gateway irraggiungibile per GET documents: {e}") from e
    if r.status_code != 200:
        raise RagStoreError(f"gateway GET documents → HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError as e:
        raise RagStoreError("gateway GET documents: risposta non JSON") from e
    docs = data.get("documents") if isinstance(data, dict) else None
    return docs if isinstance(docs, list) else []


async def list_documents_async(collection: str) -> list[dict]:
    """`list_documents` per gli handler `async def` — stessa ragione di
    `list_collections_async`."""
    return await asyncio.to_thread(list_documents, collection)


async def list_collections_async() -> list[dict]:
    """`list_collections` per gli handler `async def` (#106): la GET è
    bloccante, chiamarla dritta da un async fermerebbe l'event loop di tutto
    il processo, non solo questa richiesta."""
    return await asyncio.to_thread(list_collections)
