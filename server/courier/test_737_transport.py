"""clodia-platform#737 — the courier over a transport interface.

The courier (`courier.core`) knows no medium: it runs every transport's
reception, fires each room's trigger once with what ALL of them delivered,
and routes an outbound message by its address's scheme. An in-memory
transport goes through the same tests email does.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from . import core
from .transport import (Outbound, ReceiveResult, SendResult, Transport,
                        UnsupportedScheme, split_address)

ROOM = ("SEAL-1", "acme")


class MemoryTransport:
    """A transport with no network: an inbox per room and an outbox list."""

    def __init__(self, scheme="mem", inbox=None, fail=False):
        self.scheme, self.label = scheme, f"memoria {scheme}"
        self.inbox = {k: list(v) for k, v in (inbox or {}).items()}
        self.sent: list[Outbound] = []
        self.fail = fail

    async def receive(self, only=None):
        if self.fail:
            raise ConnectionError("down")
        rooms = [only] if only is not None else list(self.inbox)
        by_room = {r: self.inbox.pop(r, []) for r in rooms}
        n = sum(len(v) for v in by_room.values())
        return ReceiveResult(delivered=n, by_room=by_room, counters={f"{self.scheme}_in": n})

    async def send(self, message):
        self.sent.append(message)
        return SendResult(ok=True, id=str(len(self.sent)))


class _Base(unittest.TestCase):
    def setUp(self):
        p = patch.dict(core._TRANSPORTS, {}, clear=True)
        p.start()
        self.addCleanup(p.stop)
        core._CYCLE_LOCK = None
        self.fired = []

        async def fire(tier, name, items):
            self.fired.append(((tier, name), list(items)))
            return ""
        f = patch("server.scheduler.mail_triggers.fire", fire)
        f.start()
        self.addCleanup(f.stop)


class Reception(_Base):
    def test_one_turn_per_room_with_what_every_transport_delivered(self):
        core.register(MemoryTransport("mem", {ROOM: [{"from": "a"}]}))
        core.register(MemoryTransport("tg", {ROOM: [{"from": "b"}], ("SEAL-1", "x"): []}))
        out = asyncio.run(core.run_cycle())
        self.assertEqual(out["delivered"], 2)
        self.assertEqual(out["mem_in"], 1)
        self.assertEqual(self.fired, [(ROOM, [{"from": "a"}, {"from": "b"}])])

    def test_a_failing_transport_does_not_stop_the_others(self):
        core.register(MemoryTransport("mem", fail=True))
        core.register(MemoryTransport("tg", {ROOM: [{"from": "b"}]}))
        out = asyncio.run(core.run_cycle())
        self.assertEqual(out["delivered"], 1)
        self.assertEqual(out["mem_errors"], 1)
        self.assertEqual(out["errors"], 1)
        self.assertEqual(len(self.fired), 1)

    def test_check_now_reaches_only_the_room(self):
        t = MemoryTransport("mem", {ROOM: [{"from": "a"}], ("SEAL-1", "x"): [{"from": "c"}]})
        core.register(t)
        asyncio.run(core.run_cycle(only=ROOM))
        self.assertEqual([r for r, _ in self.fired], [ROOM])
        self.assertIn(("SEAL-1", "x"), t.inbox)

    def test_a_gateway_down_is_an_email_error_not_a_silent_zero(self):
        from ..api import mail_courier as MC

        def down():
            raise ConnectionError("gateway")
        with patch.object(MC, "rooms", down):
            out = asyncio.run(core.run_cycle())
        self.assertEqual((out["delivered"], out["mailto_errors"], out["errors"]), (0, 1, 1))

    def test_email_is_the_default_transport(self):
        self.assertEqual([t.scheme for t in core.transports()], ["mailto"])
        self.assertIsInstance(core.transports()[0], Transport)


class Transmission(_Base):
    def test_routed_by_scheme(self):
        mem, tg = MemoryTransport("mem"), MemoryTransport("tg")
        core.register(mem)
        core.register(tg)
        r = asyncio.run(core.send(Outbound(to="tg:12345", body="ciao", room=ROOM)))
        self.assertTrue(r.ok)
        self.assertEqual((len(mem.sent), len(tg.sent)), (0, 1))

    def test_unknown_scheme_refused(self):
        core.register(MemoryTransport("mem"))
        with self.assertRaises(UnsupportedScheme):
            asyncio.run(core.send(Outbound(to="wa:39333", body="x")))

    def test_email_transmits_only_from_a_room(self):
        # #736: email transmits; a message with no room has no mailbox and no
        # decision behind it, so it is refused before the gateway is asked.
        from .transport import TransportError
        with self.assertRaises(TransportError):
            asyncio.run(core.send(Outbound(to="mario@example.com", body="x")))

    def test_addresses(self):
        self.assertEqual(split_address("mailto:a@b.it"), ("mailto", "a@b.it"))
        self.assertEqual(split_address("a@b.it"), ("mailto", "a@b.it"))
        self.assertEqual(split_address("TG:42"), ("tg", "42"))
        for bad in ("", "nessuno"):
            with self.assertRaises(UnsupportedScheme):
                split_address(bad)

    def test_a_non_transport_is_refused(self):
        with self.assertRaises(TypeError):
            core.register(object())


if __name__ == "__main__":
    unittest.main()
