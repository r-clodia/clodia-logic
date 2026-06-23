"""Storage delle credenziali provider — backed dal VAULT del gateway (Fase 4).

Le credenziali dei provider di inferenza NON vivono più nel datadir di
clodia-logic ma nella vault del gateway clodia-tools (decisione owner: «le
credenziali devono stare nel gateway, non in logic»). Questo modulo è il backend
di `_read`/`_write`/`unlink`/`connected_provider_ids` di `api/providers.py`: tutta
la logica OAuth/refresh/login resta lì, qui cambia solo *dove* si persiste.

Modello **pure-gateway**: nessuna copia locale delle credenziali. Il runner di
clodia-logic (processo trusted-core, non un modello) parla un endpoint interno
del gateway (`/internal/providers`) autenticandosi con un token ckt1 firmato per
il principal privilegiato (default `clodia`). Il bundle non transita mai da un
modello.

Endpoint del gateway (vedi clodia-tools `server/providers_api.py`):
  GET    /internal/providers          → {"ids": [...]}
  GET    /internal/providers/{pid}    → bundle | 404
  PUT    /internal/providers/{pid}    → deposita/aggiorna
  DELETE /internal/providers/{pid}    → rimuove
"""
from __future__ import annotations

import logging
import os

import requests

from ..colony import pki

LOG = logging.getLogger("agent-server.api.provider_store")

# Principal privilegiato con cui ci si autentica al gateway per le operazioni
# sui provider. Fisso (NON il kind che sta partendo): le credenziali provider
# sono infra del trusted-core, non grant del singolo agente.
_PRINCIPAL = os.environ.get("CLODIA_PROVIDER_PRINCIPAL", "clodia")

# TTL breve: ogni operazione conia un token usa-e-getta.
_TOKEN_TTL = 300
_HTTP_TIMEOUT = 10  # secondi (connessione rete docker, deve essere svelta)


class ProviderStoreError(RuntimeError):
    """Il gateway non è raggiungibile o ha risposto con un errore."""


def _base_url() -> str:
    """Base degli endpoint provider del gateway. Derivata dall'URL MCP del
    gateway (sostituendo il suffisso `/mcp/`), override via env dedicata."""
    explicit = os.environ.get("CLODIA_TOOLS_PROVIDERS_URL")
    if explicit:
        return explicit.rstrip("/")
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/")
    base = mcp.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return f"{base}/internal/providers"


def _headers() -> dict[str, str]:
    token = pki.mint_session_token(_PRINCIPAL, ttl_seconds=_TOKEN_TTL)
    return {"Authorization": f"Bearer {token}"}


def read(pid: str) -> dict | None:
    """Bundle della credenziale provider, o None se assente (404).

    Solleva ProviderStoreError su errore di rete/HTTP (gateway giù o 5xx): il
    chiamante decide se degradare a 'assente' o propagare.
    """
    url = f"{_base_url()}/{pid}"
    try:
        r = requests.get(url, headers=_headers(), timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise ProviderStoreError(f"gateway irraggiungibile per GET {pid}: {e}") from e
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise ProviderStoreError(f"gateway GET {pid} → HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise ProviderStoreError(f"gateway GET {pid}: risposta non JSON") from e


def write(pid: str, data: dict) -> None:
    """Deposita/aggiorna il bundle nel vault del gateway. Solleva
    ProviderStoreError su qualsiasi errore (il login/refresh deve accorgersene)."""
    url = f"{_base_url()}/{pid}"
    try:
        r = requests.put(url, headers=_headers(), json=data, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise ProviderStoreError(f"gateway irraggiungibile per PUT {pid}: {e}") from e
    if r.status_code != 200:
        raise ProviderStoreError(f"gateway PUT {pid} → HTTP {r.status_code}")


def delete(pid: str) -> None:
    """Rimuove il bundle dal vault del gateway (disconnect). Idempotente."""
    url = f"{_base_url()}/{pid}"
    try:
        r = requests.delete(url, headers=_headers(), timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise ProviderStoreError(f"gateway irraggiungibile per DELETE {pid}: {e}") from e
    if r.status_code != 200:
        raise ProviderStoreError(f"gateway DELETE {pid} → HTTP {r.status_code}")


def list_ids() -> set[str]:
    """ID dei provider con credenziale presente nel vault. Una sola chiamata,
    nessun segreto trasferito. Solleva ProviderStoreError su errore."""
    try:
        r = requests.get(_base_url(), headers=_headers(), timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise ProviderStoreError(f"gateway irraggiungibile per LIST: {e}") from e
    if r.status_code != 200:
        raise ProviderStoreError(f"gateway LIST → HTTP {r.status_code}")
    try:
        return set(r.json().get("ids") or [])
    except ValueError as e:
        raise ProviderStoreError("gateway LIST: risposta non JSON") from e
