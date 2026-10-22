"""#590 — the agent-server's door to topic-scoped data: who may ask."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import topic_data as T

TOPIC = {"meta": {"owner": "davide", "participants": ["davide", "clodia"]}}


class _Base(unittest.TestCase):
    def client(self, who, admin_=False, human=True):
        app = FastAPI()
        app.include_router(T.router)
        self.sent = []
        ps = [patch("server.api.topics._principal_from_request", lambda r: who),
              patch.object(T.admin, "is_human", lambda n: human),
              patch.object(T.admin, "is_admin", lambda n: admin_),
              patch("server.api.topics._principal_clearance", lambda n: "SEAL-3"),
              patch.object(T.topics_client, "data_catalog",
                           lambda body=None: self.sent.append(body) or {
                               "entries": [{"key": "datastore:a/b", "importers": ["@admin"]},
                                           {"key": "rag:c", "importers": ["mario"]}]}),
              patch.object(T.topics_client, "topic_data_action",
                           lambda t, n, b: self.sent.append(b) or {"ok": True})]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)

        async def open_topic(t, n):
            return TOPIC
        p = patch.object(T.topics_client, "async_open_topic", open_topic)
        p.start(); self.addCleanup(p.stop)
        return TestClient(app)


class CatalogTests(_Base):
    def test_a_non_admin_sees_what_they_may_import(self):
        out = self.client("mario").get("/clodia/data-catalog").json()
        self.assertEqual([e["key"] for e in out["entries"]], ["rag:c"])
        out = self.client("davide", admin_=True).get("/clodia/data-catalog").json()
        self.assertEqual(len(out["entries"]), 2)

    def test_only_admins_write_and_the_person_travels(self):
        self.assertEqual(self.client("mario").post(
            "/clodia/data-catalog", json={"action": "sync"}).status_code, 403)
        c = self.client("davide", admin_=True)
        self.assertEqual(c.post("/clodia/data-catalog", json={"action": "x"}).status_code, 400)
        self.assertEqual(c.post("/clodia/data-catalog",
                                json={"action": "migration_plan"}).status_code, 200)
        self.assertEqual(self.sent[-1]["by"], "davide")
        self.assertIs(self.sent[-1]["by_admin"], True)

    def test_an_agent_is_refused(self):
        self.assertEqual(self.client("clodia", human=False).get(
            "/clodia/data-catalog").status_code, 403)


class ChannelDataTests(_Base):
    def test_the_owner_person_imports(self):
        with patch("server.api.channels._require_scope_owner", lambda r, m: "davide"):
            r = self.client("davide").post("/clodia/channels/SEAL-2/x/data", json={
                "action": "import", "kind": "datastore", "resource": "a/b"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((self.sent[-1]["by"], self.sent[-1]["by_clearance"]), ("davide", "SEAL-3"))

    def test_unknown_action_and_agents(self):
        with patch("server.api.channels._require_scope_owner", lambda r, m: "davide"):
            self.assertEqual(self.client("davide").post(
                "/clodia/channels/SEAL-2/x/data", json={"action": "delete"}).status_code, 400)
        with patch("server.api.channels._require_scope_owner", lambda r, m: "clodia"):
            self.assertEqual(self.client("clodia", human=False).post(
                "/clodia/channels/SEAL-2/x/data", json={"action": "import"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
