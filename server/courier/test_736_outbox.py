"""clodia-platform#736 — the courier's outbox and its transmission.

The gateway queues; a gate decision moves a recipient's waiting mail to
`queued` or `denied`; the courier's pass sends what is queued (retrying up
to three times), expires what nobody decided in 24 hours, and posts each
outcome in the room.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from . import core, outbox
from .transport import SendResult

ROOM = ("SEAL-1", "acme")


def _item(**kw):
    return {"tier": ROOM[0], "name": ROOM[1], "agent": "clodia", "to": "Mario@Acme.it",
            "subject": "Offerta", "body": "testo", "account": "studio",
            "state": "pending_gate", **kw}


class _Box(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        p = patch.object(outbox, "OUTBOX_DIR", d)
        p.start(); self.addCleanup(p.stop)
        self.posts = []

        async def post(tier, name, text):
            self.posts.append(text)
        q = patch.object(core, "_post", post)
        q.start(); self.addCleanup(q.stop)


class Queue(_Box):
    def test_enqueue_and_decide(self):
        a = outbox.enqueue(_item())
        b = outbox.enqueue(_item(to="altro@x.it"))
        self.assertEqual((a["to"], a["gate_key"]), ("mario@acme.it", "egress:email:mailto:mario@acme.it"))
        moved = outbox.decided(*ROOM, "egress:email:mailto:mario@acme.it", True)
        self.assertEqual([m["id"] for m in moved], [a["id"]])
        states = {i["id"]: i["state"] for i in outbox.list_room(*ROOM)}
        self.assertEqual(states, {a["id"]: "queued", b["id"]: "pending_gate"})
        outbox.decided(*ROOM, "egress:email:mailto:altro@x.it", False)
        self.assertEqual({i["id"]: i["state"] for i in outbox.list_room(*ROOM)}[b["id"]], "denied")

    def test_a_decision_in_another_room_moves_nothing(self):
        outbox.enqueue(_item())
        self.assertEqual(outbox.decided("SEAL-1", "other", "egress:email:mailto:mario@acme.it", True), [])

    def test_bad_items_are_refused(self):
        for bad in (_item(state="sent"), _item(to="nessuno"), _item(body=" ")):
            with self.assertRaises(ValueError):
                outbox.enqueue(bad)

    def test_the_file_is_private(self):
        outbox.enqueue(_item())
        f, = list(outbox.OUTBOX_DIR.glob("*.json"))
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)


class Transmit(_Box):
    def _transport(self, results):
        t = AsyncMock()
        t.send.side_effect = list(results)
        return patch.object(core, "transport_for", lambda to: t), t

    def test_queued_mail_is_sent_and_said_in_the_room(self):
        a = outbox.enqueue(_item(state="queued"))
        p, t = self._transport([SendResult(ok=True)])
        with p:
            out = asyncio.run(core.transmit())
        self.assertEqual(out["sent"], 1)
        msg = t.send.call_args[0][0]
        self.assertEqual((msg.to, msg.sender, msg.room), ("mailto:mario@acme.it", "studio", ROOM))
        self.assertEqual(outbox.list_room(*ROOM)[0]["state"], "sent")
        self.assertIn("📤 Inviata", self.posts[0])
        self.assertIn(a["id"], self.posts[0])

    def test_pending_mail_is_never_sent(self):
        outbox.enqueue(_item())
        p, t = self._transport([])
        with p:
            asyncio.run(core.transmit())
        t.send.assert_not_called()

    def test_a_failure_is_retried_then_given_up(self):
        outbox.enqueue(_item(state="queued"))
        p, t = self._transport([SendResult(ok=False, error="smtp giù")] * 3)
        with p:
            for _ in range(3):
                asyncio.run(core.transmit())
        i = outbox.list_room(*ROOM)[0]
        self.assertEqual((i["state"], i["attempts"]), ("failed", 3))
        self.assertEqual(len(self.posts), 1)
        self.assertIn("smtp giù", self.posts[0])

    def test_undecided_mail_expires_after_24_hours(self):
        outbox.enqueue(_item())
        p, t = self._transport([])
        with p:
            out = asyncio.run(core.transmit(now=time.time() + 25 * 3600))
        self.assertEqual(out["expired"], 1)
        self.assertEqual(outbox.list_room(*ROOM)[0]["state"], "expired")
        self.assertIn("24 ore", self.posts[0])

    def test_email_send_goes_to_the_gateway(self):
        from ..api import mail_courier as MC
        from .transport import Outbound
        got = []
        with patch.object(MC, "send_mail", lambda b: got.append(b) or {"ok": True, "detail": "inviata"}):
            r = asyncio.run(MC.EmailTransport().send(Outbound(
                to="mailto:m@a.it", body="b", subject="s", sender="studio", room=ROOM)))
        self.assertTrue(r.ok)
        self.assertEqual((got[0]["to"], got[0]["account"], got[0]["tier"]), ("m@a.it", "studio", "SEAL-1"))


class GateDecision(_Box):
    def test_the_gate_moves_the_rooms_waiting_mail(self):
        from ..api import gate as G
        a = outbox.enqueue(_item())
        asyncio.run(G._courier_decided("SEAL-1/acme", "egress:email:mailto:mario@acme.it", False))
        self.assertEqual(outbox.list_room(*ROOM)[0]["state"], "denied")
        self.assertIn("negato", self.posts[0])
        # Other gates are not the courier's.
        asyncio.run(G._courier_decided("-", "egress:web:https://x.it", True))


if __name__ == "__main__":
    unittest.main()
