"""#609 — the agent-server's door to the room's toolbox and the DM toolbox."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from . import topic_data as T

TOPIC = {"meta": {"owner": "davide", "participants": ["davide", "mario"]}}


class _Base(unittest.TestCase):
    def client(self, who, admin_=False, human=True):
        app = FastAPI()
        app.include_router(T.router)
        self.sent = []
        ps = [patch("server.api.topics._principal_from_request", lambda r: who),
              patch.object(T.admin, "is_human", lambda n: human),
              patch.object(T.admin, "is_admin", lambda n: admin_),
              patch("server.api.topics._principal_clearance", lambda n: "SEAL-3"),
              patch.object(T.topics_client, "topic_tools",
                           lambda t, n, b=None: self.sent.append(b) or {"enabled": []}),
              patch.object(T.topics_client, "dm_tools",
                           lambda b=None: self.sent.append(b) or {"enabled": []})]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)

        async def open_topic(t, n):
            return TOPIC
        p = patch.object(T.topics_client, "async_open_topic", open_topic)
        p.start(); self.addCleanup(p.stop)
        return TestClient(app)


def _member(r, meta):
    who = "mario"
    if who not in meta["participants"]:
        raise HTTPException(403, "non partecipante")
    return who


def _not_owner(r, meta):
    raise HTTPException(403, "solo l'owner")


class RoomToolsTests(_Base):
    def test_a_participant_reads_the_toolbox(self):
        with patch("server.api.channels._require_member", _member):
            r = self.client("mario").get("/clodia/channels/SEAL-1/x/tools")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self.sent[-1])

    def test_only_the_owner_changes_it_and_the_person_travels(self):
        with patch("server.api.channels._require_scope_owner", _not_owner):
            r = self.client("mario").post("/clodia/channels/SEAL-1/x/tools",
                                          json={"action": "enable", "verb": "web.fetch"})
        self.assertEqual(r.status_code, 403)
        with patch("server.api.channels._require_scope_owner", lambda r, m: "davide"):
            c = self.client("davide")
            self.assertEqual(c.post("/clodia/channels/SEAL-1/x/tools",
                                    json={"action": "svuota"}).status_code, 400)
            r = c.post("/clodia/channels/SEAL-1/x/tools",
                       json={"action": "except", "verb": "web.post", "agent": "mario"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((self.sent[-1]["by"], self.sent[-1]["agent"]), ("davide", "mario"))

    def test_the_mcp_actions_reach_the_gateway(self):
        """#620: whole MCPs, resource rows and levels travel with the person."""
        with patch("server.api.channels._require_scope_owner", lambda r, m: "davide"):
            c = self.client("davide")
            for body in ({"action": "enable_mcp", "mcp": "google"},
                         {"action": "set_resource", "mcp": "google",
                          "uri": "gdrive:folder/F1", "effect": "allow"},
                         {"action": "unset_resource", "mcp": "google", "uri": "gdrive:folder/F1"},
                         {"action": "except_mcp", "mcp": "google", "agent": "mario",
                          "level": "read"},
                         {"action": "disable_mcp", "mcp": "google"}):
                self.assertEqual(c.post("/clodia/channels/SEAL-1/x/tools", json=body).status_code,
                                 200, body)
                self.assertEqual(self.sent[-1]["by"], "davide")

    def test_an_agent_never_passes(self):
        self.assertEqual(self.client("clodia", human=False).get(
            "/clodia/channels/SEAL-1/x/tools").status_code, 403)


class DmToolsTests(_Base):
    def test_any_person_reads_an_admin_writes(self):
        self.assertEqual(self.client("mario").get("/clodia/dm-tools").status_code, 200)
        self.assertEqual(self.client("mario").post(
            "/clodia/dm-tools", json={"action": "enable", "verb": "web.fetch"}).status_code, 403)
        r = self.client("davide", admin_=True).post(
            "/clodia/dm-tools", json={"action": "enable", "verb": "web.fetch"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.sent[-1]["by"], "davide")


if __name__ == "__main__":
    unittest.main()
