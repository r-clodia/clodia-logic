"""Test selezione risponditore del canale (rango + tag + clearance)."""
from __future__ import annotations

import asyncio
import unittest

from ..agents.models import AgentSpec
from ..core.models import MessageRequest
from . import channels


def _a(name, type="normal", clearance="P0", created_at=None, role=None) -> AgentSpec:
    return AgentSpec.model_validate({
        "name": name, "description": "d", "display_name": name, "type": type,
        "clearance": clearance, "created_at": created_at, "role": role,
        **({"model": "m", "system_prompt": "s.md"} if type != "human" else {}),
    })


class ResponderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agents = {
            "clodia": _a("clodia", "super", "P3", "2026-01-01T00:00:00Z"),
            "ophelia": _a("ophelia", "super", "P3", "2026-01-01T00:00:01Z"),
            "worker": _a("worker", "normal", "P1", "2026-02-01T00:00:00Z"),
            "owner": _a("owner", "human", role="superadmin"),
        }
        self._orig = channels.registry.get_by_name
        channels.registry.get_by_name = lambda n: self.agents.get(n)

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig

    def test_highest_rank_ai_responds(self) -> None:
        r = channels._pick_responder(["owner", "worker", "clodia"], "P0", None)
        self.assertEqual(r.name, "clodia")  # super > normal; umano non risponde

    def test_seniority_clodia_over_ophelia(self) -> None:
        r = channels._pick_responder(["ophelia", "clodia"], "P0", None)
        self.assertEqual(r.name, "clodia")

    def test_tag_overrides_rank(self) -> None:
        r = channels._pick_responder(["clodia", "worker"], "P0", "worker")
        self.assertEqual(r.name, "worker")

    def test_clearance_excludes_low(self) -> None:
        # canale P2: worker (P1) escluso, clodia (P3) ok
        r = channels._pick_responder(["worker", "clodia"], "P2", None)
        self.assertEqual(r.name, "clodia")
        # canale P2 con solo worker (P1) → nessun risponditore
        self.assertIsNone(channels._pick_responder(["worker"], "P2", None))

    def test_tag_low_clearance_falls_back(self) -> None:
        # worker taggato ma clearance insufficiente (P2) → escluso → fallback clodia
        r = channels._pick_responder(["worker", "clodia"], "P2", "worker")
        self.assertEqual(r.name, "clodia")

    def test_tag_parse(self) -> None:
        self.assertEqual(channels._tagged("ehi @worker puoi farlo?"), "worker")
        self.assertIsNone(channels._tagged("nessun tag qui"))

    def test_channel_meta_defaults_to_clodia(self) -> None:
        meta = channels._channel_meta({"title": "Aiuto"}, "owner", "support")
        self.assertEqual(meta["contact_agent"], "clodia")
        self.assertEqual(meta["participants"], ["owner", "clodia"])

    def test_channel_meta_uses_requested_contact_agent(self) -> None:
        meta = channels._channel_meta(
            {"title": "Aiuto", "type": "infra", "contact_agent": "Helpdesk"},
            "owner",
            "support",
        )
        self.assertEqual(meta["contact_agent"], "helpdesk")
        self.assertEqual(meta["participants"], ["owner", "helpdesk"])
        self.assertEqual(meta["type"], "infra")


class ChannelQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.agent = _a("clodia", "super", "P3", "2026-01-01T00:00:00Z")
        self.posts: list[tuple[str, str, str]] = []
        self.sent = asyncio.Event()
        self.release = asyncio.Event()
        self._orig_principal = channels._principal_from_request
        self._orig_open_topic = channels.topics_client.open_topic
        self._orig_list_messages = channels.topics_client.list_messages
        self._orig_post_message = channels.topics_client.post_message
        self._orig_touch = channels.access_log.touch
        self._orig_activity = channels.activity_log.append
        self._orig_pick = channels._pick_responder
        self._orig_manager_get = channels.manager.get
        self._orig_manager_create = channels.manager.create
        self._orig_channel_message = channels._channel_message
        self._orig_typing = channels._typing

        class FakeChat:
            principal = ""

            async def send_user_message(chat_self, _prompt: str) -> str:
                self.sent.set()
                await self.release.wait()
                return "risposta"

        async def create(**_kwargs):
            return FakeChat()

        async def noop_async(*_args, **_kwargs):
            return None

        channels._principal_from_request = lambda _request: "owner"
        channels.topics_client.open_topic = lambda _tier, _name: {
            "meta": {"tier": "P0", "owner": "owner", "participants": ["owner", "clodia"]}
        }
        channels.topics_client.list_messages = lambda *_args, **_kwargs: []
        channels.topics_client.post_message = (
            lambda _tier, _name, author, text, kind="human", **_kwargs:
                self.posts.append((author, text, kind))
        )
        channels.access_log.touch = lambda *_args, **_kwargs: None
        channels.activity_log.append = lambda *_args, **_kwargs: None
        channels._pick_responder = lambda *_args, **_kwargs: self.agent
        channels.manager.get = lambda _chat_id: (_ for _ in ()).throw(KeyError(_chat_id))
        channels.manager.create = create
        channels._channel_message = noop_async
        channels._typing = noop_async

    async def asyncTearDown(self) -> None:
        channels._principal_from_request = self._orig_principal
        channels.topics_client.open_topic = self._orig_open_topic
        channels.topics_client.list_messages = self._orig_list_messages
        channels.topics_client.post_message = self._orig_post_message
        channels.access_log.touch = self._orig_touch
        channels.activity_log.append = self._orig_activity
        channels._pick_responder = self._orig_pick
        channels.manager.get = self._orig_manager_get
        channels.manager.create = self._orig_manager_create
        channels._channel_message = self._orig_channel_message
        channels._typing = self._orig_typing

    async def test_channel_post_queues_responder_without_waiting_for_reply(self) -> None:
        res = await channels.channel_post("P0", "ops", MessageRequest(content="@clodia vai"), object())

        self.assertTrue(res["posted"])
        self.assertTrue(res["queued"])
        self.assertEqual(res["responder"], "clodia")
        self.assertEqual(self.posts, [("owner", "@clodia vai", "human")])

        await self.sent.wait()
        self.assertEqual(self.posts, [("owner", "@clodia vai", "human")])

        self.release.set()
        for _ in range(10):
            if len(self.posts) > 1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.posts[-1], ("clodia", "risposta", "ai"))


if __name__ == "__main__":
    unittest.main()
