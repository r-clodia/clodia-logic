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
            "SEAL-1", "ops", "controlla", interval_minutes=30, agent="clodia",
        )
        self.assertEqual(db.get_topic_trigger("SEAL-1", "ops")["id"], trigger["id"])
        with self.assertRaises(Exception):
            db.create_topic_trigger(
                "SEAL-1", "ops", "di nuovo", interval_minutes=60)

    def test_topic_trigger_keeps_optional_agent_empty(self):
        trigger = db.create_topic_trigger(
            "SEAL-1", "ops", "controlla", interval_minutes=30)
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
            interval_minutes=10, prompt="controlla", agent="clodia",
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
            interval_minutes=10, prompt="controlla", agent="ophelia",
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
            "SEAL-1", "ops", "controlla", interval_minutes=10,
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
        db.create_topic_trigger("SEAL-1", "ops", "controlla", interval_minutes=10)
        db.create_job("globale", "0 * * * *", "esegui")

        jobs = await api.api_list_jobs()

        self.assertEqual([job["name"] for job in jobs], ["globale"])

    async def test_put_rejects_interval_below_10_minute_floor(self):
        # issue #46: ogni fire è un turno agentico → floor di 10 min. Il floor
        # è lo stesso di prima, cambia solo la forma in cui è espresso (#239).
        for minuti in (1, 5, 9):
            body = api.TopicCronTriggerUpsert(
                interval_minutes=minuti, prompt="controlla", agent="clodia")
            with mock.patch.object(api, "_caller", return_value="davide"), \
                 mock.patch.object(api.topics_client, "open_topic", return_value=self.topic), \
                 mock.patch.object(api, "_require_valid_agent"):
                with self.assertRaises(Exception) as ctx:
                    await api.api_put_topic_cron_trigger("SEAL-1", "ops", body, object())
                self.assertEqual(getattr(ctx.exception, "status_code", None), 422)
        self.assertEqual(db.list_jobs(), [])  # niente creato

    async def test_put_rejects_cron_expr_with_a_reason(self):
        # Un client vecchio che manda ancora il cron riceve un 422 che lo dice.
        # Ignorarlo in silenzio creerebbe un trigger a cadenza non richiesta.
        body = api.TopicCronTriggerUpsert(
            interval_minutes=30, prompt="x", agent="clodia",
            cron_expr="*/30 * * * *")
        with mock.patch.object(api, "_caller", return_value="davide"), \
             mock.patch.object(api.topics_client, "open_topic", return_value=self.topic), \
             mock.patch.object(api, "_require_valid_agent"):
            with self.assertRaises(Exception) as ctx:
                await api.api_put_topic_cron_trigger("SEAL-1", "ops", body, object())
            self.assertEqual(getattr(ctx.exception, "status_code", None), 422)
            self.assertIn("interval_minutes", str(ctx.exception.detail))
        self.assertEqual(db.list_jobs(), [])

    async def test_put_stores_interval_and_repetitions(self):
        # Criterio di fine #239: la rilettura espone i due campi, non il cron.
        body = api.TopicCronTriggerUpsert(
            interval_minutes=30, repeat_count=4, prompt="controlla", agent="clodia")
        with mock.patch.object(api, "_caller", return_value="davide"), \
             mock.patch.object(api.topics_client, "open_topic", return_value=self.topic), \
             mock.patch.object(api, "_require_valid_agent"), \
             mock.patch.object(api.scheduler, "register_job"):
            await api.api_put_topic_cron_trigger("SEAL-1", "ops", body, object())
            letto = await api.api_get_topic_cron_trigger("SEAL-1", "ops", object())

        trigger = letto["trigger"]
        self.assertEqual(trigger["interval_minutes"], 30)
        self.assertEqual(trigger["repeat_count"], 4)
        self.assertEqual(trigger["fired_count"], 0)
        self.assertFalse(trigger["cron_expr"])

    async def test_get_suggests_interval_for_legacy_cron_without_rewriting_it(self):
        # Migrazione NON silenziosa: il record legacy resta a cron, la GET
        # allega solo la cadenza da proporre nel form.
        legacy = db.create_job(
            "topic-trigger:SEAL-1/ops", "*/30 * * * *", "controlla",
            agent="", mode="topic_trigger", topic_tier="SEAL-1", topic_name="ops")
        with mock.patch.object(api, "_caller", return_value="davide"), \
             mock.patch.object(api.topics_client, "open_topic", return_value=self.topic):
            letto = await api.api_get_topic_cron_trigger("SEAL-1", "ops", object())

        self.assertEqual(letto["trigger"]["suggested_interval_minutes"], 30)
        self.assertEqual(letto["trigger"]["cron_expr"], "*/30 * * * *")
        self.assertIsNone(db.get_job(legacy["id"])["interval_minutes"])
        self.assertEqual(db.get_job(legacy["id"])["cron_expr"], "*/30 * * * *")

    async def test_saving_over_a_legacy_trigger_replaces_the_cron(self):
        db.create_job(
            "topic-trigger:SEAL-1/ops", "*/30 * * * *", "controlla",
            agent="", mode="topic_trigger", topic_tier="SEAL-1", topic_name="ops")
        body = api.TopicCronTriggerUpsert(
            interval_minutes=45, repeat_count=4, prompt="controlla", agent="clodia")
        with mock.patch.object(api, "_caller", return_value="davide"), \
             mock.patch.object(api.topics_client, "open_topic", return_value=self.topic), \
             mock.patch.object(api, "_require_valid_agent"), \
             mock.patch.object(api.scheduler, "register_job"):
            result = await api.api_put_topic_cron_trigger("SEAL-1", "ops", body, object())

        self.assertEqual(result["trigger"]["interval_minutes"], 45)
        self.assertEqual(result["trigger"]["cron_expr"], "")
        self.assertEqual(len(db.list_jobs()), 1)  # sostituito, non duplicato

    async def test_saving_rearms_an_exhausted_trigger(self):
        trigger = db.create_topic_trigger(
            "SEAL-1", "ops", "controlla", interval_minutes=30, repeat_count=2)
        db.count_fire(trigger["id"])
        db.count_fire(trigger["id"])
        self.assertFalse(db.get_job(trigger["id"])["enabled"])

        body = api.TopicCronTriggerUpsert(
            interval_minutes=30, repeat_count=2, prompt="controlla")
        with mock.patch.object(api, "_caller", return_value="davide"), \
             mock.patch.object(api.topics_client, "open_topic", return_value=self.topic), \
             mock.patch.object(api.scheduler, "register_job"):
            result = await api.api_put_topic_cron_trigger("SEAL-1", "ops", body, object())

        # Stessi numeri, ma è un riarmo: riparte da 0, non già oltre il limite.
        self.assertTrue(result["trigger"]["enabled"])
        self.assertEqual(result["trigger"]["fired_count"], 0)

    async def test_non_owner_cannot_manage_trigger(self):
        # owner=davide; un participant non-owner (clodia) o estraneo → 403 su
        # GET/PUT/DELETE.
        body = api.TopicCronTriggerUpsert(
            interval_minutes=10, prompt="x", agent="clodia")
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
            "SEAL-1", "ops", "controlla", interval_minutes=30, agent="clodia",
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
            "SEAL-1", "ops", "routing standard", interval_minutes=30,
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
            "SEAL-1", "ops", "controlla", interval_minutes=10, agent="clodia",
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


class TopicTriggerScheduleTests(unittest.TestCase):
    """Il trigger a intervallo NON passa da un cron derivato (#239)."""

    def _gap_minutes(self, trigger) -> float:
        from datetime import datetime, timedelta
        now = datetime.now(scheduler._SCHED_TZ)
        primo = trigger.get_next_fire_time(None, now)
        secondo = trigger.get_next_fire_time(primo, primo + timedelta(seconds=1))
        return (secondo - primo).total_seconds() / 60.0

    def test_interval_45_really_fires_every_45_minutes(self):
        trigger = scheduler._trigger_for(
            {"interval_minutes": 45, "cron_expr": ""})
        self.assertAlmostEqual(self._gap_minutes(trigger), 45.0, places=3)

    def test_the_cron_shortcut_would_have_been_wrong(self):
        # Perché non deriviamo `*/45 * * * *` da «ogni 45 minuti»: quel cron
        # fira ai minuti 0 e 45, cioè un fire su due arriva dopo 15 minuti.
        # Questo test è la ragione della scelta, scritta in modo eseguibile.
        self.assertEqual(scheduler.min_cron_gap_minutes("*/45 * * * *"), 15.0)

    def test_legacy_cron_record_still_uses_the_cron_trigger(self):
        from apscheduler.triggers.cron import CronTrigger
        trigger = scheduler._trigger_for(
            {"interval_minutes": None, "cron_expr": "0 9 * * *"})
        self.assertIsInstance(trigger, CronTrigger)

    def test_cron_to_interval_is_best_effort_and_admits_defeat(self):
        self.assertEqual(scheduler.cron_to_interval_minutes("*/30 * * * *"), 30)
        self.assertEqual(scheduler.cron_to_interval_minutes("0 * * * *"), 60)
        # Cadenza irregolare (lun-ven: 24h,24h,24h,24h,72h): nessun intervallo
        # singolo la rappresenta, meglio non proporne uno.
        self.assertIsNone(scheduler.cron_to_interval_minutes("0 9 * * 1-5"))
        # Annuale: «ogni 525600 minuti» non è un suggerimento, è un numero da
        # ricontrollare a mano.
        self.assertIsNone(scheduler.cron_to_interval_minutes("0 9 1 1 *"))
        self.assertIsNone(scheduler.cron_to_interval_minutes("non-una-cron"))


class TopicTriggerRepetitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = db.JOBS_DIR
        db.JOBS_DIR = Path(self.tmp.name)

    async def asyncTearDown(self) -> None:
        db.JOBS_DIR = self.old_dir
        self.tmp.cleanup()

    async def _fire(self, job_id: int, *, busy: bool = False):
        from ..api import channels
        post = mock.AsyncMock(return_value={"queued": True, "responders": ["clodia"]})
        with mock.patch.object(channels, "_responder_busy", return_value=busy), \
             mock.patch.object(channels, "post_channel_message", post), \
             mock.patch.object(scheduler, "unregister_job") as unregister:
            result = await scheduler.fire_job(job_id)
        return result, unregister

    async def test_trigger_disables_itself_after_the_last_repetition(self):
        trigger = db.create_topic_trigger(
            "SEAL-1", "ops", "controlla", interval_minutes=30, repeat_count=3,
            agent="clodia")
        for atteso in (1, 2):
            _, unregister = await self._fire(trigger["id"])
            self.assertEqual(db.get_job(trigger["id"])["fired_count"], atteso)
            self.assertTrue(db.get_job(trigger["id"])["enabled"])
            unregister.assert_not_called()

        result, unregister = await self._fire(trigger["id"])

        self.assertTrue(result["exhausted"])
        self.assertEqual(db.get_job(trigger["id"])["fired_count"], 3)
        self.assertFalse(db.get_job(trigger["id"])["enabled"])
        unregister.assert_called_once_with(trigger["id"])

    async def test_a_skipped_fire_does_not_consume_a_repetition(self):
        # Lo skip-if-busy non ha postato niente nel topic: «ripeti 4 volte»
        # sono 4 messaggi, non 4 tentativi.
        trigger = db.create_topic_trigger(
            "SEAL-1", "ops", "controlla", interval_minutes=30, repeat_count=1,
            agent="clodia")
        result, unregister = await self._fire(trigger["id"], busy=True)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(db.get_job(trigger["id"])["fired_count"], 0)
        self.assertTrue(db.get_job(trigger["id"])["enabled"])
        unregister.assert_not_called()

    async def test_repeat_count_zero_runs_forever(self):
        # Il caso "ricorrente senza fine" che il cron copriva: resta.
        trigger = db.create_topic_trigger(
            "SEAL-1", "ops", "sorveglia", interval_minutes=30, repeat_count=0,
            agent="clodia")
        for _ in range(5):
            await self._fire(trigger["id"])

        job = db.get_job(trigger["id"])
        self.assertEqual(job["fired_count"], 5)
        self.assertTrue(job["enabled"])

    async def test_changing_the_cadence_restarts_the_count(self):
        trigger = db.create_topic_trigger(
            "SEAL-1", "ops", "controlla", interval_minutes=30, repeat_count=4)
        await self._fire(trigger["id"])
        self.assertEqual(db.get_job(trigger["id"])["fired_count"], 1)

        db.update_job(trigger["id"], interval_minutes=60)

        self.assertEqual(db.get_job(trigger["id"])["fired_count"], 0)


if __name__ == "__main__":
    unittest.main()
