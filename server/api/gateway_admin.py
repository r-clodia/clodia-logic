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


def _root_url() -> str:
    """La radice del gateway, senza il suffisso `/internal/agents`.

    `_base_url()` è nato puntando all'API degli agent e ci sono rotte interne
    che non stanno lì (i connettori, #519). Si ricava dalla stessa variabile
    d'ambiente invece di aggiungerne una seconda: due URL configurabili per lo
    stesso gateway sono due occasioni di puntare a due posti diversi.
    """
    base = _base_url()
    return base[: -len("/internal/agents")] if base.endswith("/internal/agents") \
        else base


def _headers() -> dict[str, str]:
    """Come `topics_client._headers`: chiamate dell'agent-server per sé.

    `role: agent-server` serve a `/internal/connectors/validate`, che vive
    dietro il controllo ADMIN delle rotte Tools: quel controllo smette di
    accettare i token runtime di clodia/ophelia (clodia-platform#539 §5) e
    senza questo claim l'import di un pack con `connectors:` si fermerebbe.
    """
    token = pki.mint_session_token(_PRINCIPAL, ttl_seconds=_TOKEN_TTL,
                                   role=pki.ROLE_AGENT_SERVER)
    return {"Authorization": f"Bearer {token}"}


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


def connectors_validate(pack: str, connectors: list) -> dict:
    """Chiede al gateway cosa farebbe il blocco `connectors:` di un pack.

    Gemella di `flow_allow(validate=True)` e per la stessa ragione: la
    grammatica dei connettori, il registry e le liste vivono nel gateway
    (clodia-platform#516/#518). Una seconda copia dei criteri da questo lato
    divergerebbe, e divergerebbe in silenzio — per di più dalla parte in cui
    gira il codice degli agenti.

    Non concede niente e non installa niente: risponde `{ok, errors, ignored,
    connectors}`. `ok: false` è un manifest sbagliato e ferma l'installazione;
    `ignored` sono dichiarazioni corrette ma inerti su QUESTA istanza, perché il
    gateway implementa ancora da sé quel connettore — e quelle non la fermano.
    """
    payload = {"pack": pack, "connectors": list(connectors or [])}
    r = requests.post(f"{_root_url()}/internal/connectors/validate",
                      headers=_headers(), json=payload, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def registration(agent: str) -> dict:
    """La entry REGISTRATA nel gateway per `agent`, alla lettera.

    Il termine di paragone della dichiarazione del seed (clodia-platform#203).
    Non si usa `agent_verbs` al suo posto: quella è la vista da pannello —
    espande i wildcard, salta i grant che sono anche `denied` — e una lista
    ricostruita di lì diverge dall'originale proprio nei casi che a un
    rilevatore di divergenze interessano.
    """
    r = requests.get(f"{_base_url()}/{agent}/registration", headers=_headers(),
                     timeout=_HTTP_TIMEOUT)
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


async def registration_async(agent: str) -> dict:
    return await asyncio.to_thread(registration, agent)
