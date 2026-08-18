"""Client verso gli endpoint connettori del gateway (Fase 2).

Stesso pattern di topics_client: il runner di clodia-logic fa da proxy per la
webui, autenticandosi al gateway con un token ckt1 del principal `clodia`.
"""
from __future__ import annotations

import asyncio
import os

import requests

from ..colony import pki
from .gateway_http import GatewayHTTP

_PRINCIPAL = os.environ.get("CLODIA_PROVIDER_PRINCIPAL", "clodia")
_TTL = 300
_TIMEOUT = 5  # servizio interno: budget corto, come topics_client

_gw_http = GatewayHTTP("connectors")


class ConnectorsClientError(RuntimeError):
    pass


def _base() -> str:
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/").rstrip("/")
    if mcp.endswith("/mcp"):
        mcp = mcp[: -len("/mcp")]
    return f"{mcp}/internal/connectors"


def _headers() -> dict:
    return {"Authorization": f"Bearer {pki.mint_session_token(_PRINCIPAL, ttl_seconds=_TTL)}"}


def list_connectors(agent: str | None = None) -> list[dict]:
    params = {"agent": agent} if agent else None
    try:
        r = _gw_http.get(_base(), headers=_headers(), params=params, timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise ConnectorsClientError(f"gateway connectors irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise ConnectorsClientError(f"gateway connectors → HTTP {r.status_code}: {r.text[:160]}")
    return r.json().get("connectors", [])


def grant(agent: str, account: str, granted: bool) -> dict:
    try:
        r = _gw_http.post(f"{_base()}/grant", headers=_headers(),
                          json={"agent": agent, "account": account, "granted": granted},
                          timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise ConnectorsClientError(f"gateway grant irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise ConnectorsClientError(f"gateway grant → HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


async def list_connectors_async(agent: str | None = None) -> list[dict]:
    return await asyncio.to_thread(list_connectors, agent)


async def grant_async(agent: str, account: str, granted: bool) -> dict:
    return await asyncio.to_thread(grant, agent, account, granted)
