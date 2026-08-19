"""Client interno verso gli endpoint Telegram del gateway (`/internal/telegram`).

Come `provider_store`/`topics_client`: si autentica col token ckt1 del principal
`clodia`. Usato SOLO dal channel-adapter server-side (mai da un modello). Il token
del bot resta nel vault del gateway: qui passano solo chat_id e testo.
"""
from __future__ import annotations

import asyncio
import logging
import os

import requests

from ..colony import pki

LOG = logging.getLogger("agent-server.telegram_client")

_PRINCIPAL = os.environ.get("CLODIA_PROVIDER_PRINCIPAL", "clodia")
_TOKEN_TTL = 300
_HTTP_TIMEOUT = 20


def _base_url() -> str:
    explicit = os.environ.get("CLODIA_TOOLS_TELEGRAM_URL")
    if explicit:
        return explicit.rstrip("/")
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/")
    base = mcp.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return f"{base}/internal/telegram"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {pki.mint_session_token(_PRINCIPAL, ttl_seconds=_TOKEN_TTL)}"}


def updates(chat_id: str) -> dict:
    """Drena i messaggi in coda per una chat (inbound). {messages, count}."""
    r = requests.post(f"{_base_url()}/updates", headers=_headers(),
                      json={"chat_id": str(chat_id)}, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def send(chat_id: str, text: str) -> dict:
    """Invia un messaggio al gruppo (outbound)."""
    r = requests.post(f"{_base_url()}/send", headers=_headers(),
                      json={"chat_id": str(chat_id), "text": text}, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def poll(timeout: int = 25) -> list:
    """LONG-POLL verso il gateway: blocca fino a un nuovo messaggio (o timeout) e
    ritorna i messaggi nuovi di TUTTE le chat. HTTP timeout > del long-poll."""
    r = requests.post(f"{_base_url()}/poll", headers=_headers(),
                      json={"timeout": timeout}, timeout=timeout + 15)
    r.raise_for_status()
    return r.json().get("messages", [])


def download(file_id: str) -> dict:
    """Scarica un file di Telegram via il gateway. Ritorna {content_b64, size}."""
    r = requests.post(f"{_base_url()}/download", headers=_headers(),
                      json={"file_id": file_id}, timeout=90)
    r.raise_for_status()
    return r.json()


# ── wrapper async ────────────────────────────────────────────────────────────
# Il client è sincrono di proposito (lo usa anche il relay in un thread suo); da
# un handler async si passa da qui, così l'attesa di rete non ferma l'event loop.
async def send_async(chat_id: str, text: str) -> dict:
    return await asyncio.to_thread(send, chat_id, text)


async def download_async(file_id: str) -> dict:
    return await asyncio.to_thread(download, file_id)


async def updates_async(chat_id: str) -> dict:
    return await asyncio.to_thread(updates, chat_id)
