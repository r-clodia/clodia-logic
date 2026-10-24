"""#609 R5 — the agent-server's door to the room's toolbox: who may ask.

The gateway admits this side's calls without re-deriving the role (it trusts
the signed `agent-server` claim, `_authorize_topic_owner` / `dm_tools_route`).
So every refusal that matters happens HERE, before the call — and a test that
only checks the 200 would not notice if the check disappeared. Hence: for each
refusal, the gateway client must also have stayed UNTOUCHED.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import topic_tools as T

TOPIC = {"meta": {"owner": "davide", "participants": ["davide", "redattore"]}}


class _Base(unittest.TestCase):
    def client(self, who, admin_=False, human=True):
        app = FastAPI()
        app.include_router(T.router)
        self.room = []
        self.dm = []
        ps = [patch("server.api.topics._principal_from_request", lambda r: who),
              patch.object(T.admin, "is_human", lambda n: human),
              patch.object(T.admin, "is_admin", lambda n: admin_),
              patch.object(T.topics_client, "topic_tools",
                           lambda t, n, body=None: self.room.append(body) or {"ok": True}),
              patch.object(T.topics_client, "dm_tools",
                           lambda body=None: self.dm.append(body) or {"ok": True})]

        async def open_topic(t, n):
            return TOPIC
        ps.append(patch.object(T.topics_client, "async_open_topic", open_topic))
        for p in ps:
            p.start()
            self.addCleanup(p.stop)
        return TestClient(app)


class DmToolboxTests(_Base):
    """The list of verbs enabled outside a room is an instance setting: no
    topic owns it, so there is no owner to ask — only an admin."""

    def test_a_non_admin_does_not_write_it(self):
        c = self.client("mario", admin_=False)
        r = c.post("/clodia/dm-tools", json={"action": "enable", "verb": "web.fetch"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.dm, [], "il gateway non va nemmeno chiamato")

    def test_an_admin_writes_it_and_the_person_travels(self):
        c = self.client("davide", admin_=True)
        r = c.post("/clodia/dm-tools", json={"action": "enable", "verb": "web.fetch"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.dm[-1]["verb"], "web.fetch")
        self.assertEqual((self.dm[-1]["by"], self.dm[-1]["by_admin"]), ("davide", True))

    def test_the_body_cannot_promote_whoever_sends_it(self):
        c = self.client("mario", admin_=True)
        c.post("/clodia/dm-tools", json={"action": "disable", "verb": "web.fetch",
                                         "by": "davide", "by_admin": False})
        self.assertEqual((self.dm[-1]["by"], self.dm[-1]["by_admin"]), ("mario", True))

    def test_an_agent_is_refused_on_both_verbs(self):
        c = self.client("redattore", admin_=True, human=False)
        self.assertEqual(c.get("/clodia/dm-tools").status_code, 403)
        self.assertEqual(c.post("/clodia/dm-tools",
                                json={"action": "enable", "verb": "web.fetch"}).status_code, 403)
        self.assertEqual(self.dm, [])

    def test_a_person_reads_it_without_being_an_admin(self):
        """Reading is the settings panel: it shows what is enabled, and
        enabling is the act that is reserved."""
        c = self.client("mario", admin_=False)
        self.assertEqual(c.get("/clodia/dm-tools").status_code, 200)
        self.assertEqual(self.dm, [None])

    def test_an_unknown_action_stops_here(self):
        c = self.client("davide", admin_=True)
        self.assertEqual(c.post("/clodia/dm-tools", json={"action": "x"}).status_code, 400)
        self.assertEqual(self.dm, [])


class RoomToolboxTests(_Base):
    def test_the_owner_enables_a_verb_in_their_room(self):
        with patch("server.api.channels._require_scope_owner", lambda r, m: "davide"):
            r = self.client("davide").post("/clodia/channels/SEAL-1/blog/tools",
                                           json={"action": "enable", "verb": "web.fetch"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.room[-1]["by"], "davide")

    def test_whoever_does_not_own_the_room_is_refused(self):
        from fastapi import HTTPException

        def refuse(r, m):
            raise HTTPException(403, "non sei owner")
        with patch("server.api.channels._require_scope_owner", refuse):
            r = self.client("mario").post("/clodia/channels/SEAL-1/blog/tools",
                                          json={"action": "enable", "verb": "web.fetch"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.room, [])

    def test_an_admin_who_does_not_own_the_room(self):
        def not_owner(r, m):
            raise AssertionError("a un admin non si chiede di possedere il topic")
        with patch("server.api.channels._require_scope_owner", not_owner):
            r = self.client("davide", admin_=True).post(
                "/clodia/channels/SEAL-1/blog/tools",
                json={"action": "except", "verb": "web.fetch", "agent": "redattore"})
        self.assertEqual(r.status_code, 200)

    def test_an_agent_does_not_move_this_wall(self):
        c = self.client("redattore", admin_=True, human=False)
        self.assertEqual(c.get("/clodia/channels/SEAL-1/blog/tools").status_code, 403)
        self.assertEqual(c.post("/clodia/channels/SEAL-1/blog/tools",
                                json={"action": "enable", "verb": "web.fetch"}).status_code, 403)
        self.assertEqual(self.room, [])

    def test_the_panel_reads_the_toolbox(self):
        with patch("server.api.channels._require_scope_owner", lambda r, m: "davide"):
            r = self.client("davide").get("/clodia/channels/SEAL-1/blog/tools")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.room, [None])

    def test_an_unknown_action_stops_here(self):
        with patch("server.api.channels._require_scope_owner", lambda r, m: "davide"):
            r = self.client("davide").post("/clodia/channels/SEAL-1/blog/tools",
                                           json={"action": "grant", "verb": "web.fetch"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.room, [])

    def test_a_missing_room_is_404(self):
        async def none(t, n):
            return None
        c = self.client("davide", admin_=True)
        with patch.object(T.topics_client, "async_open_topic", none):
            self.assertEqual(c.get("/clodia/channels/SEAL-1/ghost/tools").status_code, 404)


class WiringTests(unittest.TestCase):
    def test_the_routes_are_mounted_on_the_assembled_app(self):
        """`app.routes` is not the place to look since FastAPI 0.115 (see
        `test_no_hook_surface`): the OpenAPI of the assembled app is."""
        from .. import main
        paths = main.create_app().openapi()["paths"]
        self.assertIn("/clodia/channels/{tier}/{name}/tools", paths)
        self.assertIn("/clodia/dm-tools", paths)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
