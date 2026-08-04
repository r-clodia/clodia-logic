"""Client interno verso il gateway per la registrazione whitelist degli agent
(auto-provisioning dei responder confinati). Auth ckt1 principal clodia.
"""
from __future__ import annotations

import logging
import os

import requests

from ..colony import pki

LOG = logging.getLogger("agent-server.gateway_admin")

_PRINCIPAL = os.environ.get("CLODIA_PROVIDER_PRINCIPAL", "clodia")
_TOKEN_TTL = 300
_HTTP_TIMEOUT = 15


def _base_url() -> str:
    explicit = os.environ.get("CLODIA_TOOLS_AGENTS_URL")
    if explicit:
        return explicit.rstrip("/")
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/")
    base = mcp.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return f"{base}/internal/agents"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {pki.mint_session_token(_PRINCIPAL, ttl_seconds=_TOKEN_TTL)}"}


def register_agent(agent: str, allowed_tools: list | None = None) -> dict:
    """Registra/aggiorna l'agent nella whitelist del gateway (config.yaml)."""
    r = requests.post(f"{_base_url()}/whitelist", headers=_headers(),
                      json={"agent": agent, "allowed_tools": allowed_tools or []},
                      timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def flow_allow(flows: dict, source: str = "", validate: bool = False) -> dict:
    """Convalida (`validate=True`) o concede le dichiarazioni di flusso di un pack.

    Il gateway è l'unico posto in cui le due liste possono essere scritte: qui non
    si tiene una copia dei criteri, si chiede. Una seconda copia divergerebbe, e
    divergerebbe in silenzio.
    """
    payload = {"source": source, "validate": bool(validate),
               "egress": list(flows.get("egress") or []),
               "ingress": list(flows.get("ingress") or [])}
    r = requests.post(f"{_base_url()}/flow-allow", headers=_headers(),
                      json=payload, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()
