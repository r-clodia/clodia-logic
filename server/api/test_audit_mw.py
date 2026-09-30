"""clodia-platform#439 (part 2) and #444 (part 2): one middleware records the
agent-server's control-plane changes and people's sessions."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import agents, audit_mw
from .. import audit_events

PERSON = {"agent": "davide", "iat": 1790000000, "exp": 1790003600, "human_role": "admin"}


def _app() -> FastAPI:
    app = FastAPI()
    audit_mw.install(app)

    @app.post("/api/providers/{pid}/pause")
    def pause(pid: str):
        return {"paused": pid}

    @app.get("/api/providers")
    def listing():
        return []

    @app.post("/clodia/channels/{tier}/{name}/messages")
    def chat(tier: str, name: str):
        return {}

    @app.delete("/clodia/packs/{name}")
    def remove(name: str):
        return {"removed": name}
    return app


class MiddlewareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[dict] = []
        audit_mw._sessions.clear()
        self.claims = PERSON
        for p in (patch.object(audit_events, "_bg", self.events.append),
                  patch.object(agents, "_verified_claims", lambda r: self.claims)):
            p.start()
            self.addCleanup(p.stop)
        self.c = TestClient(_app())
        self.h = {"authorization": "Bearer ckt1.x.y"}

    def control(self) -> list[dict]:
        return [e for e in self.events if e["type"].startswith("control.")]

    def test_a_mutation_on_a_control_surface_is_recorded_with_who_and_what(self) -> None:
        self.c.post("/api/providers/aws-region-eu/pause", headers=self.h)
        (ev,) = self.control()
        self.assertEqual(ev["type"], "control.provider")
        self.assertEqual(ev["resource"], "/api/providers/{pid}/pause")
        self.assertEqual(ev["result"]["path_params"], {"pid": "aws-region-eu"})
        self.assertEqual(ev["result"]["status_code"], 200)
        self.assertEqual(ev["actor"], {"type": "principal", "id": "davide", "role": "admin"})

    def test_reads_and_conversation_are_not_control_events(self) -> None:
        self.c.get("/api/providers", headers=self.h)
        self.c.post("/clodia/channels/SEAL-1/t/messages", headers=self.h)
        self.assertEqual(self.control(), [])

    def test_a_pack_removal_is_recorded(self) -> None:
        self.c.delete("/clodia/packs/studio-legale", headers=self.h)
        self.assertEqual(self.control()[0]["type"], "control.pack")

    def test_a_session_is_one_event_per_token_not_per_request(self) -> None:
        for _ in range(4):
            self.c.get("/api/providers", headers=self.h)
        self.claims = {**PERSON, "iat": PERSON["iat"] + 100}   # a new session token
        self.c.get("/api/providers", headers=self.h)
        sessions = [e for e in self.events if e["type"] == "human.session"]
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0]["actor"]["id"], "davide")

    def test_an_agents_own_token_is_not_a_human_session(self) -> None:
        self.claims = {"agent": "clodia", "execution_id": "clodia-3", "iat": 1}
        self.c.get("/api/providers", headers=self.h)
        self.assertEqual([e for e in self.events if e["type"] == "human.session"], [])

    def test_the_response_is_never_changed_by_auditing(self) -> None:
        with patch.object(audit_mw, "control_event", side_effect=RuntimeError("bug")):
            r = self.c.post("/api/providers/x/pause", headers=self.h)
        self.assertEqual(r.json(), {"paused": "x"})


if __name__ == "__main__":
    unittest.main()
