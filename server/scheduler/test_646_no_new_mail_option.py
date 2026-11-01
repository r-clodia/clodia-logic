"""#646 — the cron trigger's «only if new mail» option (#586) is gone.

Reacting to mail is the onMessageReceived trigger's job. A client that still
asks for the option is refused with a reason; an old record carrying it fires
on its cadence and loses the field at its next write.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from . import api, db, scheduler


class NoNewMailOptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = db.JOBS_DIR
        db.JOBS_DIR = Path(self.tmp.name)
        self.topic = {"meta": {"owner": "davide", "participants": ["davide", "clodia"]}}

    async def asyncTearDown(self) -> None:
        db.JOBS_DIR = self.old_dir
        self.tmp.cleanup()

    async def _put(self, body):
        with mock.patch.object(api, "_caller", return_value="davide"), \
             mock.patch.object(api.topics_client, "open_topic", return_value=self.topic), \
             mock.patch.object(api, "_require_valid_agent"), \
             mock.patch.object(api.scheduler, "register_job"):
            return await api.api_put_topic_cron_trigger("SEAL-1", "ops", body, object())

    async def test_asking_for_the_option_is_refused_with_a_reason(self):
        body = api.TopicCronTriggerUpsert(
            interval_minutes=10, prompt="controlla", agent="clodia", only_if_new_mail=True)
        with self.assertRaisesRegex(Exception, "onMessageReceived"):
            await self._put(body)
        self.assertEqual(db.list_jobs(), [])

    async def test_false_or_absent_is_accepted(self):
        for flag in (False, None):
            with self.subTest(flag=flag):
                body = api.TopicCronTriggerUpsert(
                    interval_minutes=10, prompt="controlla", agent="clodia",
                    only_if_new_mail=flag)
                out = await self._put(body)
                self.assertNotIn("only_if_new_mail", out["trigger"])

    async def test_an_old_record_fires_and_loses_the_field(self):
        t = db.create_topic_trigger("SEAL-1", "ops", "controlla", interval_minutes=10,
                                    agent="clodia")
        path = next(Path(self.tmp.name).glob("*.yaml"))
        rec = yaml.safe_load(path.read_text())
        rec.update({"only_if_new_mail": True, "mail_seen": ["a.eml"]})
        path.write_text(yaml.safe_dump(rec))
        posted = []

        async def post(*a, **k):
            posted.append((a, k))
            return {"status": "ok"}

        from ..api import channels
        with mock.patch.object(channels, "_responder_busy", return_value=False), \
             mock.patch.object(channels, "post_channel_message", side_effect=post, create=True), \
             mock.patch.object(scheduler, "_mailbox_names", create=True,
                               side_effect=AssertionError("mailbox must not be read")):
            try:
                await scheduler._fire_topic_trigger(db.get_job(t["id"]))
            except AssertionError:
                raise
            except Exception:  # noqa: BLE001 — the post path is not under test
                pass
        rec = yaml.safe_load(path.read_text())
        self.assertNotIn("only_if_new_mail", rec)
        self.assertNotIn("mail_seen", rec)


if __name__ == "__main__":
    unittest.main()
