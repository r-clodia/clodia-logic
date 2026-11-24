"""The courier itself (clodia-platform#737): one pass over every transport,
then one turn per room with what arrived.

It knows no medium. The transports registered here do the reception and,
from #736, the transmission; this module serialises the passes, fires each
room's trigger once per pass, and routes an outbound message to the
transport of its address's scheme.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .transport import (Outbound, ReceiveResult, SendResult, Transport,
                        UnsupportedScheme, split_address)

LOG = logging.getLogger("agent-server.courier")

_TRANSPORTS: dict[str, Transport] = {}


def register(transport: Transport) -> None:
    """Add (or replace) the transport for its scheme."""
    if not isinstance(transport, Transport):
        raise TypeError(f"{transport!r} non implementa Transport")
    _TRANSPORTS[transport.scheme] = transport


def unregister(scheme: str) -> None:
    _TRANSPORTS.pop(scheme, None)


def transports() -> list[Transport]:
    """The registered transports; email is there by default."""
    if not _TRANSPORTS:
        _register_defaults()
    return list(_TRANSPORTS.values())


def _register_defaults() -> None:
    from ..api.mail_courier import EmailTransport
    register(EmailTransport())


def transport_for(address: str) -> Transport:
    scheme, _ = split_address(address)
    for t in transports():
        if t.scheme == scheme:
            return t
    raise UnsupportedScheme(f"nessun trasporto per '{scheme}:'")


#: One courier pass at a time: the periodic loop and an agent's «check now»
#: (`topic.mail_check`, owner 14 Nov 2026) must not read the same source
#: concurrently, or a message would be delivered twice.
_CYCLE_LOCK: "asyncio.Lock | None" = None


def _lock() -> asyncio.Lock:
    global _CYCLE_LOCK
    if _CYCLE_LOCK is None:
        _CYCLE_LOCK = asyncio.Lock()
    return _CYCLE_LOCK


async def run_cycle(only: Optional[tuple] = None) -> dict:
    """One courier pass over every transport — or over ONE room (`only`).
    Returns the pass's counters (`delivered`, plus each transport's own)."""
    async with _lock():
        return await _run_cycle(only)


async def _run_cycle(only: Optional[tuple] = None) -> dict:
    total = 0
    counters: dict = {}
    per_room: dict = {}
    for t in transports():
        try:
            res: ReceiveResult = await t.receive(only)
        except Exception as e:  # noqa: BLE001 — one transport never stops the others
            LOG.warning("courier: %s reception failed (%s)", t.scheme, type(e).__name__)
            counters[f"{t.scheme}_errors"] = counters.get(f"{t.scheme}_errors", 0) + 1
            continue
        total += int(res.delivered or 0)
        for k, v in (res.counters or {}).items():
            if isinstance(v, (int, float)):
                counters[k] = counters.get(k, 0) + v
        for room, items in (res.by_room or {}).items():
            per_room.setdefault(tuple(room), []).extend(items or [])
    await fire_triggers(per_room)
    return {"delivered": total, **counters}


async def fire_triggers(per_room: dict) -> None:
    """ONE turn per room per pass, whatever the number of its sources (#628).
    The courier never starts a turn itself (no model on its path): the
    trigger's owner side does, like the scheduler's topic trigger."""
    for (tier, name), items in per_room.items():
        if not items:
            continue
        try:
            from ..scheduler import mail_triggers
            await mail_triggers.fire(tier, name, items)
        except Exception as e:  # noqa: BLE001 — a trigger never stops the courier
            LOG.warning("mail trigger %s/%s: %s", tier, name, type(e).__name__)


async def send(message: Outbound) -> SendResult:
    """Hand one AUTHORISED message to the transport of its address. The gate
    and the queue in front of this are #736's; nothing calls it before."""
    return await transport_for(message.to).send(message)
