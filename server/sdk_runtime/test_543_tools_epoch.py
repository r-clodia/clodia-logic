"""clodia-platform#543 — a grant reaches the session that is already alive.

The Claude CLI reads the gateway's `tools/list` once, when its client opens,
and the gateway does not emit `tools/list_changed`. On 15 Oct `buffer.*` was
granted and four mentions in a row reached the same live spawn, which never
saw it. Now a change of what agents may reach bumps an epoch, and a session
whose client is older reopens it before its next turn.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import session as S
from .test_session_recovery import _make_session


class _Epochs(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(S._TOOLS_EPOCH, clear=True)
        p.start()
        self.addCleanup(p.stop)


class EpochTests(_Epochs):
    def _opened(self, chat_id: str):
        sess = _make_session()
        sess.chat_id = chat_id
        sess._opened_tools_epoch = S._tools_epoch(chat_id)
        return sess

    def test_a_change_in_the_room_concerns_its_sessions_only(self):
        here = self._opened("chan:SEAL-1:blog:clodia")
        there = self._opened("chan:SEAL-2:altro:clodia")
        S.bump_tools_epoch("SEAL-1/blog")
        self.assertTrue(here._tools_changed())
        self.assertFalse(there._tools_changed())

    def test_a_global_change_concerns_every_session(self):
        room = self._opened("chan:SEAL-1:blog:clodia")
        dm = self._opened("dm:davide:clodia")
        S.bump_tools_epoch(None)
        self.assertTrue(room._tools_changed())
        self.assertTrue(dm._tools_changed())

    def test_a_session_never_opened_has_nothing_to_refresh(self):
        sess = _make_session()
        S.bump_tools_epoch(None)
        self.assertFalse(sess._tools_changed())


class TheNextTurnReopens(_Epochs, unittest.IsolatedAsyncioTestCase):

    async def _turn(self, sess) -> list:
        causes = []

        async def recover():
            causes.append(sess._recover_cause)
            sess._opened_tools_epoch = S._tools_epoch(sess.chat_id)
            return True

        async def _audit(_s):
            return None

        client = mock.AsyncMock()
        client.query.side_effect = RuntimeError("stop here")   # only the preamble matters
        sess._client = client
        with mock.patch.object(S, "_audit_turn_acquired", _audit), \
                mock.patch.object(S.activity_log, "append"), \
                mock.patch.object(sess, "_record", new=mock.AsyncMock()), \
                mock.patch.object(sess, "_publish_error", new=mock.AsyncMock()), \
                mock.patch.object(sess, "_set_status", new=mock.AsyncMock()), \
                mock.patch.object(sess, "_refresh_provider_env", return_value=False), \
                mock.patch.object(sess, "_refresh_mcp_principal", return_value=False), \
                mock.patch.object(sess, "_recover_session", side_effect=recover):
            with self.assertRaises(Exception):
                await sess.send_user_message("ciao")
        return causes

    async def test_after_a_grant_the_turn_starts_on_a_fresh_client(self):
        sess = _make_session()
        sess._opened_tools_epoch = S._tools_epoch(sess.chat_id)
        S.bump_tools_epoch("SEAL-1/test")
        causes = await self._turn(sess)
        # "tools_changed" first; the failed query then recovers as it always did.
        self.assertEqual(causes[0], "tools_changed")

    async def test_without_a_change_the_client_is_kept(self):
        sess = _make_session()
        sess._opened_tools_epoch = S._tools_epoch(sess.chat_id)
        causes = await self._turn(sess)
        self.assertNotIn("tools_changed", causes)


class TheRoutesBumpIt(_Epochs):
    def _client(self):
        from ..api import topic_data
        app = FastAPI()
        app.include_router(topic_data.router)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(topic_data, "_room_person",
                          new=mock.AsyncMock(return_value="davide")).start()
        mock.patch.object(topic_data, "_person", return_value="davide").start()
        mock.patch.object(topic_data.admin, "is_admin", return_value=True).start()
        mock.patch.object(topic_data, "_actor", return_value={"by": "davide"}).start()
        mock.patch.object(topic_data.topics_client, "topic_tools",
                          return_value={"ok": True}).start()
        mock.patch.object(topic_data.topics_client, "dm_tools",
                          return_value={"ok": True}).start()
        mock.patch.object(topic_data.topics_client, "authz_rule_remove",
                          return_value=True).start()
        return TestClient(app)

    def test_the_room_toolbox(self):
        r = self._client().post("/clodia/channels/SEAL-1/blog/tools",
                                json={"action": "enable_mcp", "mcp": "buffer"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(S._TOOLS_EPOCH, {"SEAL-1/blog": 1})

    def test_the_verbs_outside_rooms(self):
        r = self._client().post("/clodia/dm-tools", json={"action": "enable", "verb": "x.y"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(S._TOOLS_EPOCH, {"*": 1})

    def test_a_removed_rule(self):
        c = self._client()
        self.assertEqual(c.delete("/clodia/channels/SEAL-1/blog/rules/r1").status_code, 200)
        self.assertEqual(c.delete("/clodia/authz/rules/r2").status_code, 200)
        self.assertEqual(S._TOOLS_EPOCH, {"SEAL-1/blog": 1, "*": 1})

    def test_a_refused_change_bumps_nothing(self):
        from ..api import topic_data
        c = self._client()
        mock.patch.object(topic_data.topics_client, "topic_tools",
                          side_effect=topic_data.topics_client.TopicsClientError("no")).start()
        r = c.post("/clodia/channels/SEAL-1/blog/tools", json={"action": "enable", "verb": "a.b"})
        self.assertGreaterEqual(r.status_code, 400)
        self.assertEqual(S._TOOLS_EPOCH, {})


if __name__ == "__main__":
    unittest.main()
