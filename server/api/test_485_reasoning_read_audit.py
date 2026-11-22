"""clodia-platform#485 — reading a bubble's stored reasoning is on the trail.

Reasoning quotes the channel (#484), so on a topic ≥ SEAL-2 reading it is
read evidence like the gateway's `human.view` of the topic (#444). The store
lives on the agent-server, so the gateway never sees the read: the
agent-server reports it, allowed or refused. Below SEAL-2 nothing is recorded,
as #444 decided for the topic views.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from . import channels
from .. import audit_events

_ENTRY = {"message_id": "m1", "spawn": "clodia-7", "text": "thinking", "truncated": False}


def _get(tier: str, *, member: bool = True, entry=_ENTRY) -> tuple[int, list[dict]]:
    app = FastAPI()
    app.include_router(channels.router)
    sent: list[dict] = []
    guard = ({"return_value": "davide"} if member else
             {"side_effect": HTTPException(403, "not a participant")})
    with patch.object(channels.topics_client, "async_open_topic",
                      new=AsyncMock(return_value={"meta": {"tier": tier}})), \
         patch.object(channels, "_require_member", **guard), \
         patch.object(channels, "_principal_from_request",
                      return_value="davide" if member else "mario"), \
         patch.object(channels.reasoning_log, "read", return_value=entry), \
         patch.object(audit_events, "_bg", sent.append):
        r = TestClient(app).get(f"/clodia/channels/{tier}/dossier/reasoning/m1")
    return r.status_code, sent


class ReadsOnTheTrail(unittest.TestCase):
    def test_a_read_on_seal_2_is_recorded(self):
        status, sent = _get("SEAL-2")
        self.assertEqual(status, 200)
        self.assertEqual(sent, [{
            "type": "human.view", "action": "open",
            "resource": "reasoning:SEAL-2/dossier/m1",
            "scope": {"tier": "SEAL-2", "topic": "dossier"},
            "actor": {"type": "human", "id": "davide"},
            "result": {"what": "reasoning", "message_id": "m1", "outcome": "read"}}])

    def test_a_refused_read_is_recorded_as_denied(self):
        status, sent = _get("SEAL-3", member=False)
        self.assertEqual(status, 403)
        self.assertEqual([(e["actor"]["id"], e["result"]["outcome"]) for e in sent],
                         [("mario", "denied")])

    def test_a_missing_entry_is_recorded_as_not_found(self):
        status, sent = _get("SEAL-2", entry=None)
        self.assertEqual(status, 404)
        self.assertEqual([e["result"]["outcome"] for e in sent], ["not_found"])

    def test_below_seal_2_nothing_is_recorded(self):
        for tier in ("SEAL-0", "SEAL-1"):
            with self.subTest(tier=tier):
                self.assertEqual(_get(tier), (200, []))
                self.assertEqual(_get(tier, member=False), (403, []))

    def test_the_threshold_is_the_gateways_variable(self):
        with patch.object(audit_events, "READS_MIN_TIER", "SEAL-1"):
            self.assertIsNotNone(audit_events.reasoning_read("SEAL-1", "x", "m", "d", "read"))
        self.assertIsNone(audit_events.reasoning_read("P1", "x", "m", "d", "read"))
        self.assertIsNotNone(audit_events.reasoning_read("P2", "x", "m", "d", "read"))

    def test_the_gateway_admits_the_type(self):
        """The ingest route takes only the agent-server's prefixes; `human.`
        is one of them (clodia-tools `AGENT_SERVER_TYPES`)."""
        ev = audit_events.reasoning_read("SEAL-2", "x", "m", None, "denied")
        self.assertTrue(ev["type"].startswith("human."))
        self.assertEqual(ev["actor"], {"type": "anonymous"})


if __name__ == "__main__":
    unittest.main()
