"""Relay adapters for chat transports declared by packs (clodia-platform#638).

A connector of a pack may declare a `relay:` block: its transport is a service
of the same pack, reachable by the gateway only on loopback. The agent-server
never talks to that service. It talks to the gateway's generic channel routes,
which forward to the declared service for a connector the registry accepted and
validate the URIs of every row:

- `POST /internal/channels/{connector}/poll {timeout}` → `{messages: [row…]}`;
  each row has the relay's inbound shape (see `relay_adapters`) plus
  `chat_uri` and `sender_uri`, already checked against the connector's schemes;
- `POST /internal/channels/{connector}/send {chat_id, text, thread_id?}` →
  `{ok, message_id}`;
- `POST /internal/channels/{connector}/download {file_id}` →
  `{content_b64, size, mime}`.

One generic adapter (`GatewayChannelAdapter`) serves every such connector. The
relay model — buffer, activation, sender vetting, refusal, chat whitelist —
stays the one of `channel_relay`; this module only adds the transport.

The set of connectors with a relay is a projection of `GET /internal/connectors`
cached for `_TTL` seconds. A gateway failure never propagates: the last known
projection is kept (empty at boot), so a registry outage leaves Telegram — a
built-in that does not depend on this table — working.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from urllib.parse import quote

import requests

from . import topics_client
from .relay_adapters import RelayAdapter

LOG = logging.getLogger("agent-server.pack_relay_adapter")

_HTTP_TIMEOUT = 20
_DOWNLOAD_TIMEOUT = 90
#: Seconds a registry projection stays valid; also the retry period after a
#: failed fetch, so an unreachable gateway is asked once a minute, not per cycle.
_TTL = 60.0


def _channels_url(connector: str, action: str) -> str:
    return (f"{topics_client._internal_base()}/channels/"
            f"{quote(str(connector), safe='')}/{action}")


def _post(connector: str, action: str, body: dict, timeout: float) -> dict:
    """Same identity as every other call the agent-server makes to the gateway
    for itself: a ckt1 token of the `clodia` principal with `role: agent-server`
    (`topics_client._headers`)."""
    r = requests.post(_channels_url(connector, action),
                      headers=topics_client._headers(), json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


class GatewayChannelAdapter(RelayAdapter):
    """A pack transport, reached only through the gateway's channel routes."""

    post_kind = "channel"

    def __init__(self, connector: str, label: str = "", schemes=(),
                 attachments: bool = False):
        self.connector = str(connector)
        self._chat_uris: dict[str, str] = {}
        self.configure(label=label, schemes=schemes, attachments=attachments)

    def configure(self, label: str = "", schemes=(), attachments: bool = False) -> None:
        """(Re)apply the `relay:` block. Kept separate from `__init__` so a
        registry refresh updates the instance in place and the chat URIs it
        has learnt from the polled rows survive the refresh."""
        schemes = [str(s) for s in (schemes or ()) if str(s or "").strip()]
        self.transport_label = str(label or self.connector)
        self.uri_scheme = schemes[0] if schemes else ""
        self.schemes = schemes
        self.attachments = bool(attachments)

    def post_fields(self) -> dict:
        return {"transport": self.transport_label}

    # ── inbound / outbound ───────────────────────────────────────────────────
    def _poll_sync(self, timeout: int) -> list:
        data = _post(self.connector, "poll", {"timeout": timeout}, timeout + 15)
        rows = [m for m in (data or {}).get("messages") or [] if isinstance(m, dict)]
        for m in rows:
            cid, uri = m.get("chat_id"), str(m.get("chat_uri") or "").strip()
            if cid is not None and uri:
                self._chat_uris[str(cid)] = uri
        return rows

    async def poll(self, timeout: int) -> list:
        return await asyncio.to_thread(self._poll_sync, timeout)

    async def send(self, chat_id, text: str, thread_id=None) -> None:
        body = {"chat_id": str(chat_id), "text": text}
        if thread_id:
            body["thread_id"] = thread_id
        await asyncio.to_thread(_post, self.connector, "send", body, _HTTP_TIMEOUT)

    async def download(self, file_ref: dict) -> str | None:
        if not self.attachments:
            return None
        data = await asyncio.to_thread(
            _post, self.connector, "download",
            {"file_id": (file_ref or {}).get("file_id")}, _DOWNLOAD_TIMEOUT)
        return (data or {}).get("content_b64")

    # ── identity ─────────────────────────────────────────────────────────────
    def sender_uri(self, msg: dict) -> str | None:
        """The gateway validated it; a row without one is not vettable."""
        uri = str((msg or {}).get("sender_uri") or "").strip()
        return uri or None

    def chat_uri(self, chat_id) -> str | None:
        """The `chat_uri` of the last polled row of that chat. This adapter
        does not build URIs: a chat it has never seen polled is not vettable
        (None → refused by the relay)."""
        return self._chat_uris.get(str(chat_id))


# ── registry projection ───────────────────────────────────────────────────────
_LOCK = threading.Lock()
_ADAPTERS: dict[str, GatewayChannelAdapter] = {}
_FETCHED_AT: float | None = None
_FAILING = False


def _relay_rows(registry: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in (registry or {}).get("connectors") or ():
        if not isinstance(row, dict):
            continue
        cid, relay = str(row.get("id") or ""), row.get("relay")
        if cid and isinstance(relay, dict):
            out[cid] = relay
    return out


def refresh(force: bool = False) -> None:
    """Refresh the projection if stale. Never raises."""
    global _FETCHED_AT, _FAILING
    with _LOCK:
        now = time.monotonic()
        if not force and _FETCHED_AT is not None and now - _FETCHED_AT < _TTL:
            return
        _FETCHED_AT = now
        try:
            rows = _relay_rows(topics_client.connectors_registry())
        except Exception as e:  # noqa: BLE001 — a registry outage must not kill the relay
            if not _FAILING:
                LOG.warning("connector registry unavailable (%s): keeping %d pack "
                            "relay transport(s) as last known", str(e)[:160], len(_ADAPTERS))
            _FAILING = True
            return
        if _FAILING:
            LOG.info("connector registry reachable again")
        _FAILING = False
        for cid in list(_ADAPTERS):
            if cid not in rows:
                _ADAPTERS.pop(cid, None)
        for cid, relay in rows.items():
            kw = {"label": relay.get("label") or "",
                  "schemes": relay.get("schemes") or (),
                  "attachments": bool(relay.get("attachments"))}
            if cid in _ADAPTERS:
                _ADAPTERS[cid].configure(**kw)
            else:
                _ADAPTERS[cid] = GatewayChannelAdapter(cid, **kw)


async def refresh_async(force: bool = False) -> None:
    """The same, off the event loop (the fetch is a blocking HTTP call)."""
    await asyncio.to_thread(refresh, force)


def lookup(connector: str):
    """The adapter of a connector declaring `relay:`, or None."""
    refresh()
    return _ADAPTERS.get(str(connector or ""))


def reset() -> None:
    """Forget the projection (tests)."""
    global _FETCHED_AT, _FAILING
    with _LOCK:
        _ADAPTERS.clear()
        _FETCHED_AT = None
        _FAILING = False
