"""Client verso il gateway per leggere credenziali git (PAT) dal vault.

Usato dai workflow (engine) per clonare/pushare repo privati. Auth ckt1
principal trusted-core, come provider_store. Il PAT non transita mai da un
modello e non viene loggato.
"""
from __future__ import annotations

import os

import requests

from ..colony import pki

_PRINCIPAL = os.environ.get("CLODIA_PROVIDER_PRINCIPAL", "clodia")
_TOKEN_TTL = 300
_HTTP_TIMEOUT = 15


def _base_url() -> str:
    explicit = os.environ.get("CLODIA_TOOLS_VAULT_URL")
    if explicit:
        return explicit.rstrip("/")
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/")
    base = mcp.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return f"{base}/internal/vault"


def read_credential(name: str) -> str | None:
    """Valore della credenziale `name` dal vault del gateway (o None)."""
    try:
        r = requests.get(
            f"{_base_url()}/{name}",
            headers={"Authorization": f"Bearer {pki.mint_session_token(_PRINCIPAL, ttl_seconds=_TOKEN_TTL)}"},
            timeout=_HTTP_TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    return (r.json() or {}).get("value") or None
