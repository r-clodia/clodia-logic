"""Client interno verso il gateway per la registrazione whitelist degli agent
(auto-provisioning dei responder confinati). Auth ckt1 principal clodia.
"""
from __future__ import annotations

import logging
import os
import asyncio

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


def register_agent(agent: str, allowed_tools: list | None = None,
                   gated_tools: list | None = None,
                   gated_in_channel: list | None = None,
                   carries: list | None = None,
                   profile_tools: list | None = None,
                   denied_tools: list | None = None) -> dict:
    """Registra/aggiorna l'agent nella whitelist del gateway (config.yaml).

    `gated_tools` viaggia con la registrazione perché è dichiarato nel seed e
    custodito dal gateway: la dichiarazione sta dove si sa quali verbi sono
    pericolosi, l'autorità dove l'agente non può riscriverla.
    """
    # `gated_tools` si OMETTE quando non c'è, non si manda `[]`: il gateway tratta
    # l'assenza come «non mi pronuncio» e la lista vuota come «azzerale». Mandare
    # sempre `[]` sconfiggeva quella guardia dal lato client — e ha azzerato i gate
    # di clodia al primo update del base-pack, cioè ha ALLARGATO l'autorità di un
    # agente immutabile con un aggiornamento che doveva solo cambiargli il prompt.
    payload: dict = {"agent": agent, "allowed_tools": allowed_tools or []}
    if gated_tools is not None:
        payload["gated_tools"] = list(gated_tools)
    # Stessa regola dell'omissione: mandare `[]` sempre toglierebbe il gate del
    # canale a ogni registrazione, che è come sono spariti i gate di clodia.
    if gated_in_channel is not None:
        payload["gated_in_channel"] = list(gated_in_channel)
    if carries is not None:
        payload["carries"] = list(carries)
    if profile_tools is not None:
        payload["profile_tools"] = list(profile_tools)
    if denied_tools is not None:
        payload["denied_tools"] = list(denied_tools)
    r = requests.post(f"{_base_url()}/whitelist", headers=_headers(),
                      json=payload, timeout=_HTTP_TIMEOUT)
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


def agent_verbs(agent: str) -> dict:
    """Verbi EFFETTIVI dell'agent col flag gated, dal gateway.

    Non si costruisce qui: il gateway è l'unico che conosce insieme il catalogo
    dei verbi nativi, la lista gated globale, i `gated_tools` per-agente e i
    `denied_tools`. Una risposta assemblata da questo lato sarebbe una seconda
    verità, e divergerebbe come è già divergiuto lo specchio dei denied.
    """
    r = requests.get(f"{_base_url()}/{agent}/verbs", headers=_headers(),
                     timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


async def register_agent_async(agent: str, allowed_tools: list | None = None,
                               gated_tools: list | None = None,
                               gated_in_channel: list | None = None,
                               carries: list | None = None,
                               profile_tools: list | None = None,
                               denied_tools: list | None = None) -> dict:
    return await asyncio.to_thread(
        register_agent,
        agent,
        allowed_tools,
        gated_tools,
        gated_in_channel,
        carries,
        profile_tools,
        denied_tools,
    )


async def flow_allow_async(flows: dict, source: str = "", validate: bool = False) -> dict:
    return await asyncio.to_thread(flow_allow, flows, source, validate)


async def agent_verbs_async(agent: str) -> dict:
    return await asyncio.to_thread(agent_verbs, agent)
