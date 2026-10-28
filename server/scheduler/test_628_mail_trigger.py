"""#628 — the channel trigger «incoming mail»: store, routes, fire."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from . import api, mail_triggers as MT


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(MT, "DIR", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)


class StoreTests(_Tmp):
    def test_upsert_get_delete(self):
        MT.upsert("SEAL-1", "ops", agent="clodia", prompt="leggi la posta", owner="davide")
        t = MT.get("SEAL-1", "ops")
        self.assertEqual((t["agent"], t["prompt"], t["enabled"], t["fired_count"]),
                         ("clodia", "leggi la posta", True, 0))
        self.assertTrue(MT.delete("SEAL-1", "ops"))
        self.assertIsNone(MT.get("SEAL-1", "ops"))

    def test_absent_and_disabled_do_not_fire(self):
        self.assertEqual(MT.claim_fire("SEAL-1", "ops")[1], "absent")
        MT.upsert("SEAL-1", "ops", agent="clodia", prompt="p", enabled=False)
        self.assertEqual(MT.claim_fire("SEAL-1", "ops")[1], "disabled")

    def test_at_most_twelve_an_hour(self):
        MT.upsert("SEAL-1", "ops", agent="clodia", prompt="p")
        for i in range(MT.MAX_PER_HOUR):
            self.assertEqual(MT.claim_fire("SEAL-1", "ops", now=1000.0 + i)[1], "")
        trig, motivo = MT.claim_fire("SEAL-1", "ops", now=1100.0)
        self.assertEqual(motivo, "rate_limited")
        self.assertEqual(trig["last_status"], "rate_limited")
        self.assertEqual(trig["fired_count"], MT.MAX_PER_HOUR)
        self.assertEqual(MT.claim_fire("SEAL-1", "ops", now=1000.0 + 3601)[1], "")

    def test_a_retiered_topic_keeps_its_trigger(self):
        MT.upsert("SEAL-1", "ops", agent="clodia", prompt="p")
        self.assertTrue(MT.retier("SEAL-1", "SEAL-2", "ops"))
        self.assertIsNone(MT.get("SEAL-1", "ops"))
        self.assertEqual(MT.get("SEAL-2", "ops")["agent"], "clodia")

    def test_a_name_is_never_a_path(self):
        with self.assertRaises(ValueError):
            MT.upsert("SEAL-1", "../x", agent="clodia", prompt="p")

    def test_the_prompt_lists_the_mail(self):
        t = MT.prompt_text("clodia", "rispondi", [{"from": "a@b.it", "subject": "Ciao",
                                                     "eml": "local/mailbox/x.eml"}])
        self.assertTrue(t.startswith("@clodia rispondi"))
        self.assertIn("a@b.it — «Ciao» (local/mailbox/x.eml)", t)


class RouteTests(_Tmp):
    TOPIC = {"meta": {"owner": "davide", "participants": ["davide", "clodia", "mario"]}}

    def _run(self, coro, who="davide"):
        with mock.patch.object(api, "_caller", return_value=who), \
                mock.patch.object(api.topics_client, "open_topic", return_value=self.TOPIC), \
                mock.patch.object(api, "_require_valid_agent", lambda a: None), \
                mock.patch("server.api.admin.is_admin", lambda p: False):
            return asyncio.run(coro)

    def test_the_owner_writes_a_participant_reads(self):
        body = api.TopicMailTriggerUpsert(agent="clodia", prompt="leggi")
        out = self._run(api.api_put_topic_mail_trigger("SEAL-1", "ops", body, None))
        self.assertEqual(out["trigger"]["agent"], "clodia")
        self.assertEqual(self._run(api.api_get_topic_mail_trigger("SEAL-1", "ops", None),
                                   who="mario")["trigger"]["prompt"], "leggi")
        with self.assertRaises(HTTPException) as e:
            self._run(api.api_put_topic_mail_trigger("SEAL-1", "ops", body, None), who="mario")
        self.assertEqual(e.exception.status_code, 403)
        with self.assertRaises(HTTPException) as e:
            self._run(api.api_get_topic_mail_trigger("SEAL-1", "ops", None), who="estraneo")
        self.assertEqual(e.exception.status_code, 403)
        self.assertEqual(self._run(api.api_delete_topic_mail_trigger("SEAL-1", "ops", None)),
                         {"deleted": True})

    def test_the_agent_must_be_a_participant(self):
        body = api.TopicMailTriggerUpsert(agent="avvocato", prompt="leggi")
        with self.assertRaises(HTTPException) as e:
            self._run(api.api_put_topic_mail_trigger("SEAL-1", "ops", body, None))
        self.assertEqual(e.exception.status_code, 422)


class FireTests(_Tmp):
    def _fire(self, items, participants=("clodia",), rows=None):
        from ..api import mail_courier as MC
        posts = []

        async def post(tier, name, content, principal, **kw):
            posts.append((tier, name, content, principal, kw))
            return {"responder": "clodia"}

        async def open_topic(t, n):
            return {"meta": {"participants": list(participants)}}
        with mock.patch("server.api.channels.post_channel_message", post), \
                mock.patch("server.api.channels.indirizzo", lambda a: a), \
                mock.patch.object(MC.topics_client, "async_open_topic", open_topic):
            if rows is None:
                out = asyncio.run(MT.fire("SEAL-1", "ops", items))
            else:
                async def room_run(room, only=None, delivered=None):
                    delivered.extend(items)
                    return len(items)
                with mock.patch.object(MC, "rooms", lambda: rows), \
                        mock.patch.object(MC, "box", lambda: None), \
                        mock.patch.object(MC, "run_room", room_run):
                    asyncio.run(MC.run_cycle())
                out = None
        return out, posts

    ITEM = {"from": "a@b.it", "subject": "Ciao", "eml": "local/mailbox/x.eml"}

    def test_a_turn_with_the_mail_queued_not_skipped(self):
        MT.upsert("SEAL-1", "ops", agent="clodia", prompt="leggi e rispondi")
        out, posts = self._fire([self.ITEM])
        self.assertEqual(out, "")
        (tier, name, content, principal, kw) = posts[0]
        self.assertTrue(content.startswith("@clodia leggi e rispondi"))
        self.assertEqual((kw["kind"], kw["trusted_internal"], kw["skip_if_busy"]),
                         ("system", True, False))
        self.assertEqual(MT.get("SEAL-1", "ops")["last_status"], "ok")

    def test_one_turn_per_room_per_cycle(self):
        """A room with two linked mailboxes gets ONE turn per cycle."""
        MT.upsert("SEAL-1", "ops", agent="clodia", prompt="leggi")
        row = {"tier": "SEAL-1", "name": "ops", "account": "a", "senders": 1}
        _out, posts = self._fire([self.ITEM], rows=[row, {**row, "account": "b"}])
        self.assertEqual(len(posts), 1)
        self.assertIn("(2)", posts[0][2])

    def test_disabled_or_absent_no_turn(self):
        _out, posts = self._fire([self.ITEM])
        self.assertEqual(posts, [])
        MT.upsert("SEAL-1", "ops", agent="clodia", prompt="p", enabled=False)
        out, posts = self._fire([self.ITEM])
        self.assertEqual((out, posts), ("disabled", []))

    def test_an_agent_that_left_the_room_gets_no_turn(self):
        MT.upsert("SEAL-1", "ops", agent="clodia", prompt="p")
        out, posts = self._fire([self.ITEM], participants=("segretario",))
        self.assertEqual((out, posts), ("agent_not_participant", []))
        self.assertEqual(MT.get("SEAL-1", "ops")["last_status"], "agent_not_participant")


if __name__ == "__main__":
    unittest.main()
