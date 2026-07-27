from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from . import api, db


class _Request:
    def __init__(self, body: dict):
        self._body = body

    async def json(self) -> dict:
        return self._body


class LocalHookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir, self.old_file = db._DIR, db._FILE
        db._DIR = Path(self.tmp.name)
        db._FILE = db._DIR / "hooks.json"
        self.old_open = api.topics_client.open_topic
        self.old_post = api.topics_client.post_message
        self.old_queue = api._queue_turn
        self.posts: list[dict] = []
        self.queued: list[dict] = []
        api.topics_client.open_topic = lambda _tier, _name: {
            "meta": {
                "owner": "owner",
                "participants": ["owner", "clodia"],
                "hook_enabled": True,
            }
        }
        api.topics_client.post_message = lambda *args, **kwargs: self.posts.append(kwargs)
        api._queue_turn = lambda *args, **kwargs: self.queued.append(kwargs) or True

    async def asyncTearDown(self) -> None:
        api.topics_client.open_topic = self.old_open
        api.topics_client.post_message = self.old_post
        api._queue_turn = self.old_queue
        db._DIR, db._FILE = self.old_dir, self.old_file
        self.tmp.cleanup()

    async def test_participant_invocation_mentions_and_wakes_caller(self) -> None:
        result = await api.invoke_local(
            "SEAL-1", "acme", _Request({"caller": "clodia", "payload": "sincronizza"}))
        self.assertTrue(result["triggered"])
        self.assertEqual(self.posts[0]["text"], "@clodia sincronizza")
        self.assertEqual(self.queued[0]["responder"], "clodia")

    async def test_non_participant_is_denied(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await api.invoke_local(
                "SEAL-1", "acme", _Request({"caller": "ophelia", "payload": "x"}))
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_messaggero_can_invoke_without_membership(self) -> None:
        result = await api.invoke_local(
            "SEAL-1", "acme", _Request({"caller": "messaggero", "payload": "mail"}))
        self.assertTrue(result["triggered"])
        self.assertEqual(self.queued[0]["responder"], "messaggero")


if __name__ == "__main__":
    unittest.main()
