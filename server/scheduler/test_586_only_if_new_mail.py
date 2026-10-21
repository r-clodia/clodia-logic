"""#586 — a topic trigger «only if new mail»: the fire is skipped, with no
model call and no post, unless `local/mailbox` has `.eml` files the trigger
has not seen at its last dispatched fire."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import api, db, scheduler


class OnlyIfNewMailTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = db.JOBS_DIR
        db.JOBS_DIR = Path(self.tmp.name)
        self.mailbox = ["2026-10-21-aaaa1111.eml"]
        self.posts = []

    async def asyncTearDown(self) -> None:
        db.JOBS_DIR = self.old_dir
        self.tmp.cleanup()

    def _files(self, tier, name, subpath=""):
        self.assertEqual(subpath, "local/mailbox")
        return [{"name": n, "kind": "file"} for n in self.mailbox] + \
               [{"name": "2026-10-21-aaaa1111.txt", "kind": "file"},
                {"name": "sub", "kind": "dir"}]

    async def _post(self, tier, name, content, author, **kw):
        self.posts.append(content)
        return {"responder": "clodia"}

    async def fire(self, job_id):
        from ..api import channels
        with mock.patch("server.api.topics_client.list_files", self._files), \
                mock.patch.object(channels, "post_channel_message", self._post), \
                mock.patch.object(channels, "_responder_busy", return_value=False):
            return await scheduler._fire_topic_trigger(db.get_job(job_id))

    def _trigger(self, only=True):
        return db.create_topic_trigger("SEAL-1", "tomato-blogging", "esegui la posta",
                                       interval_minutes=10, agent="clodia",
                                       only_if_new_mail=only)

    async def test_the_first_look_records_and_skips(self):
        t = self._trigger()
        out = await self.fire(t["id"])
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(self.posts, [])
        self.assertEqual(db.get_job(t["id"])["mail_seen"], self.mailbox)

    async def test_new_mail_dispatches_once(self):
        t = self._trigger()
        await self.fire(t["id"])
        self.mailbox.append("2026-10-21-bbbb2222.eml")
        out = await self.fire(t["id"])
        self.assertEqual(out["status"], "dispatched")
        self.assertEqual(len(self.posts), 1)
        self.assertIn("esegui la posta", self.posts[0])
        out = await self.fire(t["id"])                      # nothing new since
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(len(self.posts), 1)
        self.assertIn("nessuna posta nuova", db.get_job(t["id"])["last_status"])

    async def test_without_the_option_every_fire_dispatches(self):
        t = self._trigger(only=False)
        await self.fire(t["id"])
        await self.fire(t["id"])
        self.assertEqual(len(self.posts), 2)

    async def test_an_unreadable_mailbox_skips(self):
        t = self._trigger()

        def broken(*a, **k):
            raise RuntimeError("gateway files → HTTP 502")
        from ..api import channels
        with mock.patch("server.api.topics_client.list_files", broken), \
                mock.patch.object(channels, "post_channel_message", self._post):
            out = await scheduler._fire_topic_trigger(db.get_job(t["id"]))
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(self.posts, [])

    async def test_switching_it_on_later_does_not_count_old_mail(self):
        t = self._trigger(only=False)
        db.update_job(t["id"], only_if_new_mail=True)
        out = await self.fire(t["id"])
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(self.posts, [])

    async def test_the_api_stores_the_option(self):
        body = api.TopicCronTriggerUpsert(interval_minutes=10, prompt="p",
                                          agent="clodia", only_if_new_mail=True)
        topic = {"meta": {"owner": "davide", "participants": ["davide", "clodia"]}}
        with mock.patch.object(api, "_caller", return_value="davide"), \
                mock.patch.object(api.topics_client, "open_topic", return_value=topic), \
                mock.patch.object(api, "_require_valid_agent"), \
                mock.patch.object(api.scheduler, "register_job"):
            out = await api.api_put_topic_cron_trigger("SEAL-1", "ops", body, object())
            self.assertIs(out["trigger"]["only_if_new_mail"], True)
            body.only_if_new_mail = False
            out = await api.api_put_topic_cron_trigger("SEAL-1", "ops", body, object())
        self.assertIs(out["trigger"]["only_if_new_mail"], False)
        self.assertIs(api.TopicCronTriggerUpsert(interval_minutes=10, prompt="p")
                      .only_if_new_mail, False)


if __name__ == "__main__":
    unittest.main()
