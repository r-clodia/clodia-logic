"""#676 — the room's «In ascolto» through the agent-server."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from . import topics

_URL = "/api/topics/SEAL-1/acme/listen"


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(topics.router)
    return TestClient(app)


class ListenRouteTests(unittest.TestCase):
    def test_a_participant_reads(self):
        with patch.object(topics, "_topic_for_contributor", return_value="anna"), \
             patch.object(topics.topics_client, "async_topic_listen",
                          new=AsyncMock(return_value={"kinds": [], "sources": []})) as g:
            r = _app().get(_URL)
        self.assertEqual(200, r.status_code)
        self.assertEqual(g.await_args.args, ("SEAL-1", "acme"))

    def test_only_the_owner_writes(self):
        with patch.object(topics, "_require_topic_owner",
                          side_effect=HTTPException(403, "solo l'owner")), \
             patch.object(topics.topics_client, "async_topic_listen", new=AsyncMock()) as g:
            r = _app().post(_URL, json={"action": "add"})
        self.assertEqual(403, r.status_code)
        g.assert_not_called()

    def test_the_body_is_reduced_to_the_contract(self):
        with patch.object(topics, "_require_topic_owner", return_value="davide"), \
             patch.object(topics.topics_client, "async_topic_listen",
                          new=AsyncMock(return_value={"ok": True})) as g:
            r = _app().post(_URL, json={"action": "add", "connector": "mail", "kind": "sender",
                                        "address": "a@b.it", "reply": True, "by": "x"})
        self.assertEqual(200, r.status_code)
        self.assertNotIn("by", g.await_args.args[2])

    def test_a_refusal_is_a_400(self):
        with patch.object(topics, "_require_topic_owner", return_value="davide"), \
             patch.object(topics.topics_client, "async_topic_listen",
                          new=AsyncMock(side_effect=ValueError("indirizzo richiesto"))):
            r = _app().post(_URL, json={"action": "add"})
        self.assertEqual(400, r.status_code)


if __name__ == "__main__":
    unittest.main()
