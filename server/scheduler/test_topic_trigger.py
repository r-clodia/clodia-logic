from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import api, db, scheduler


class TopicTriggerDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = db.JOBS_DIR
        db.JOBS_DIR = Path(self.tmp.name)

    def tearDown(self) -> None:
        db.JOBS_DIR = self.old_dir
        self.tmp.cleanup()

    def test_one_trigger_per_topic(self):
        trigger = db.create_topic_trigger(
            "SEAL-1", "ops", "*/5 * * * *", "controlla", agent="clodia",
        )
        self.assertEqual(db.get_topic_trigger("SEAL-1", "ops")["id"], trigger["id"])
        with self.assertRaises(Exception):
            db.create_topic_trigger("SEAL-1", "ops", "0 * * * *", "di nuovo")

    def test_topic_trigger_keeps_optional_agent_empty(self):
        trigger = db.create_topic_trigger("SEAL-1", "ops", "*/5 * * * *", "controlla")
        self.assertEqual(db.get_job(trigger["id"])["agent"], "")
        self.assertEqual(db.get_job(trigger["id"])["mode"], "topic_trigger")


class TopicTriggerApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = db.JOBS_DIR
        db.JOBS_DIR = Path(self.tmp.name)
        self.topic = {
            "meta": {
                "owner": "davide",
                "participants": ["davide", "clodia"],
            }
        }

    async def asyncTearDown(self) -> None:
        db.JOBS_DIR = self.old_dir
        self.tmp.cleanup()

    async def test_put_upserts_single_topic_trigger(self):
        body = api.TopicCronTriggerUpsert(
            cron_expr="*/10 * * * *", prompt="controlla", agent="clodia",
        )
        with mock.patch.object(api, "_caller", return_value="davide"), \
             mock.patch.object(api.topics_client, "open_topic", return_value=self.topic), \
             mock.patch.object(api, "_require_valid_agent"), \
             mock.patch.object(api.scheduler, "register_job") as register:
            created = await api.api_put_topic_cron_trigger(
                "SEAL-1", "ops", body, object(),
            )
            body.prompt = "ricontrolla"
            updated = await api.api_put_topic_cron_trigger(
                "SEAL-1", "ops", body, object(),
            )

        self.assertEqual(created["trigger"]["id"], updated["trigger"]["id"])
        self.assertEqual(updated["trigger"]["prompt"], "ricontrolla")
        self.assertEqual(len(db.list_jobs()), 1)
        self.assertEqual(register.call_count, 2)

    async def test_agent_must_participate_in_topic(self):
        body = api.TopicCronTriggerUpsert(
            cron_expr="*/10 * * * *", prompt="controlla", agent="ophelia",
        )
        with mock.patch.object(api, "_caller", return_value="davide"), \
             mock.patch.object(api.topics_client, "open_topic", return_value=self.topic), \
             mock.patch.object(api, "_require_valid_agent"):
            with self.assertRaisesRegex(Exception, "non partecipa"):
                await api.api_put_topic_cron_trigger(
                    "SEAL-1", "ops", body, object(),
                )

    async def test_delete_removes_trigger_and_unregisters_schedule(self):
        trigger = db.create_topic_trigger(
            "SEAL-1", "ops", "*/10 * * * *", "controlla",
        )
        with mock.patch.object(api, "_caller", return_value="davide"), \
             mock.patch.object(api.topics_client, "open_topic", return_value=self.topic), \
             mock.patch.object(api.scheduler, "unregister_job") as unregister:
            result = await api.api_delete_topic_cron_trigger(
                "SEAL-1", "ops", object(),
            )

        self.assertEqual(result, {"deleted": True})
        self.assertIsNone(db.get_job(trigger["id"]))
        unregister.assert_called_once_with(trigger["id"])

    async def test_topic_triggers_are_hidden_from_global_jobs_api(self):
        db.create_topic_trigger("SEAL-1", "ops", "*/10 * * * *", "controlla")
        db.create_job("globale", "0 * * * *", "esegui")

        jobs = await api.api_list_jobs()

        self.assertEqual([job["name"] for job in jobs], ["globale"])

    async def test_put_rejects_cron_below_10_minute_floor(self):
        # issue #46: ogni fire è un turno agentico → floor di 10 min.
        for expr in ("*/1 * * * *", "*/5 * * * *", "0,3,6,9 * * * *"):
            body = api.TopicCronTriggerUpsert(
                cron_expr=expr, prompt="controlla", agent="clodia")
            with mock.patch.object(api, "_caller", return_value="davide"), \
                 mock.patch.object(api.topics_client, "open_topic", return_value=self.topic), \
                 mock.patch.object(api, "_require_valid_agent"):
                with self.assertRaises(Exception) as ctx:
                    await api.api_put_topic_cron_trigger("SEAL-1", "ops", body, object())
                self.assertEqual(getattr(ctx.exception, "status_code", None), 422)
        self.assertEqual(db.list_jobs(), [])  # niente creato

    async def test_put_rejects_invalid_cron(self):
        body = api.TopicCronTriggerUpsert(
            cron_expr="non-una-cron", prompt="x", agent="clodia")
        with mock.patch.object(api, "_caller", return_value="davide"), \
             mock.patch.object(api.topics_client, "open_topic", return_value=self.topic), \
             mock.patch.object(api, "_require_valid_agent"):
            with self.assertRaises(Exception) as ctx:
                await api.api_put_topic_cron_trigger("SEAL-1", "ops", body, object())
            self.assertEqual(getattr(ctx.exception, "status_code", None), 422)

    async def test_non_owner_cannot_manage_trigger(self):
        # owner=davide; un participant non-owner (clodia) o estraneo → 403 su
        # GET/PUT/DELETE.
        body = api.TopicCronTriggerUpsert(
            cron_expr="*/10 * * * *", prompt="x", agent="clodia")
        for caller in ("clodia", "estraneo"):
            with mock.patch.object(api, "_caller", return_value=caller), \
                 mock.patch.object(api.topics_client, "open_topic", return_value=self.topic), \
                 mock.patch("server.api.admin.is_admin", return_value=False):
                for coro in (
                    api.api_get_topic_cron_trigger("SEAL-1", "ops", object()),
                    api.api_put_topic_cron_trigger("SEAL-1", "ops", body, object()),
                    api.api_delete_topic_cron_trigger("SEAL-1", "ops", object()),
                ):
                    with self.assertRaises(Exception) as ctx:
                        await coro
                    self.assertEqual(getattr(ctx.exception, "status_code", None), 403)


class TopicTriggerFireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = db.JOBS_DIR
        db.JOBS_DIR = Path(self.tmp.name)

    async def asyncTearDown(self) -> None:
        db.JOBS_DIR = self.old_dir
        self.tmp.cleanup()

    async def test_fire_posts_direct_mention_through_channel_routing(self):
        trigger = db.create_topic_trigger(
            "SEAL-1", "ops", "*/5 * * * *", "controlla", agent="clodia",
        )
        from ..api import channels
        post = mock.AsyncMock(return_value={"queued": True, "responders": ["clodia"]})
        with mock.patch.object(channels, "post_channel_message", post):
            result = await scheduler.fire_job(trigger["id"])

        post.assert_awaited_once_with(
            "SEAL-1",
            "ops",
            "@clodia controlla",
            "scheduler",
            kind="system",
            trusted_internal=True,
            skip_if_busy=True,
        )
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(db.get_job(trigger["id"])["last_status"],
                         "dispatched (messaggio postato nel topic)")

    async def test_fire_without_agent_leaves_message_untagged(self):
        trigger = db.create_topic_trigger(
            "SEAL-1", "ops", "*/5 * * * *", "routing standard",
        )
        from ..api import channels
        post = mock.AsyncMock(return_value={"queued": True, "responder": "clodia"})
        with mock.patch.object(channels, "post_channel_message", post):
            await scheduler.fire_job(trigger["id"])

        self.assertEqual(post.await_args.args[2], "routing standard")

    async def test_fire_skips_when_responder_busy(self):
        # skip-if-busy: turno precedente del responder ancora in corso → il fire
        # è saltato, NESSUN messaggio postato e NESSUN nuovo turno.
        trigger = db.create_topic_trigger(
            "SEAL-1", "ops", "*/10 * * * *", "controlla", agent="clodia",
        )
        from ..api import channels
        post = mock.AsyncMock()
        with mock.patch.object(channels, "_responder_busy", return_value=True), \
             mock.patch.object(channels, "post_channel_message", post):
            result = await scheduler.fire_job(trigger["id"])

        post.assert_not_awaited()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skipped"], ["clodia"])
        self.assertEqual(db.get_job(trigger["id"])["last_status"],
                         "skipped (turno precedente ancora in corso)")


if __name__ == "__main__":
    unittest.main()
