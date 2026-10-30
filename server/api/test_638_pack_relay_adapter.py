"""A pack's declared relay transport reaches the room through the gateway
(clodia-platform#638).

The gateway is faked at the HTTP boundary: the registry (`GET
/internal/connectors`) and the channel routes (`POST
/internal/channels/<id>/{poll,send,download}`). The relay model is the real one.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import channel_relay, pack_relay_adapter, relay_adapters, topics_client

REGISTRY = {"connectors": [
    {"id": "telegram", "schemes": [{"scheme": "tg"}]},
    {"id": "sms-demo", "schemes": [{"scheme": "sms"}],
     "relay": {"label": "sms", "attachments": False, "schemes": ["sms"]}},
    {"id": "mail", "schemes": [{"scheme": "inbox"}]},
]}

CHAT = "c-1"
ROW = {"message_id": 1, "chat_id": CHAT, "text": "@clodia riassumi",
       "from_username": "ada", "from": "Ada", "chat_title": "Demo",
       "chat_uri": "sms:group-1", "sender_uri": "sms:+391234"}


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class FakeGateway:
    """Records every channel call; answers poll with the queued rows."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls: list = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append((url, json))
        if url.endswith("/poll"):
            rows, self.rows = self.rows, []
            return _Resp({"messages": rows})
        if url.endswith("/send"):
            return _Resp({"ok": True, "message_id": "m1"})
        if url.endswith("/download"):
            return _Resp({"content_b64": "eA==", "size": 1, "mime": "text/plain"})
        raise AssertionError(url)

    def urls(self, action):
        return [(u, b) for u, b in self.calls if u.endswith(f"/{action}")]


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pack_relay_adapter.reset()
        self.addCleanup(pack_relay_adapter.reset)
        relay_adapters._WARNED.clear()
        self.registry_calls = 0
        self.registry = REGISTRY

        def _registry():
            self.registry_calls += 1
            if isinstance(self.registry, Exception):
                raise self.registry
            return self.registry

        self.gw = FakeGateway()
        for p in (patch.object(topics_client, "connectors_registry", _registry),
                  patch.object(topics_client, "_headers", lambda: {"Authorization": "x"}),
                  patch.object(pack_relay_adapter.requests, "post", self.gw.post)):
            p.start()
            self.addCleanup(p.stop)


class RegistryTests(_Base):
    def test_a_connector_declaring_relay_gets_an_adapter(self):
        a = relay_adapters.get("sms-demo")
        self.assertIsInstance(a, pack_relay_adapter.GatewayChannelAdapter)
        self.assertEqual((a.connector, a.transport_label, a.uri_scheme, a.post_kind),
                         ("sms-demo", "sms", "sms", "channel"))
        self.assertEqual(a.post_fields(), {"transport": "sms"})

    def test_a_connector_without_relay_has_none(self):
        self.assertIsNone(relay_adapters.get("mail"))

    def test_the_registry_is_cached(self):
        relay_adapters.get("sms-demo")
        relay_adapters.get("sms-demo")
        relay_adapters.get("unknown")
        self.assertEqual(self.registry_calls, 1)

    def test_telegram_keeps_its_builtin_adapter(self):
        from .telegram_relay_adapter import ADAPTER
        self.registry = {"connectors": [
            {"id": "telegram", "relay": {"label": "fake", "schemes": ["tg"]}}]}
        self.assertIs(relay_adapters.get("telegram"), ADAPTER)
        self.assertEqual(ADAPTER.post_kind, "telegram")
        self.assertEqual(ADAPTER.post_fields(), {})

    def test_a_registry_failure_is_not_fatal_and_keeps_telegram(self):
        from .telegram_relay_adapter import ADAPTER
        self.registry = RuntimeError("gateway down")
        self.assertIsNone(relay_adapters.get("sms-demo"))
        self.assertIs(relay_adapters.get("telegram"), ADAPTER)

    def test_a_registry_failure_keeps_the_last_known_adapters(self):
        a = relay_adapters.get("sms-demo")
        self.registry = RuntimeError("gateway down")
        pack_relay_adapter.refresh(force=True)
        self.assertIs(relay_adapters.get("sms-demo"), a)

    def test_a_removed_pack_loses_its_adapter(self):
        self.assertIsNotNone(relay_adapters.get("sms-demo"))
        self.registry = {"connectors": []}
        pack_relay_adapter.refresh(force=True)
        self.assertIsNone(relay_adapters.get("sms-demo"))


class AdapterTests(_Base):
    async def test_send_goes_to_the_gateway_channel_route(self):
        a = relay_adapters.get("sms-demo")
        await a.send(CHAT, "ciao", thread_id=7)
        (url, body), = self.gw.urls("send")
        self.assertTrue(url.endswith("/internal/channels/sms-demo/send"), url)
        self.assertEqual(body, {"chat_id": CHAT, "text": "ciao", "thread_id": 7})

    async def test_poll_remembers_the_chat_uri_and_uses_the_row_uris(self):
        a = relay_adapters.get("sms-demo")
        self.assertIsNone(a.chat_uri(CHAT))      # never polled → not vettable
        self.gw.rows = [dict(ROW)]
        rows = await a.poll(3)
        (url, body), = self.gw.urls("poll")
        self.assertTrue(url.endswith("/internal/channels/sms-demo/poll"), url)
        self.assertEqual(body, {"timeout": 3})
        self.assertEqual(a.chat_uri(CHAT), "sms:group-1")
        self.assertEqual(a.sender_uri(rows[0]), "sms:+391234")
        self.assertIsNone(a.sender_uri({"from_username": "ada"}))

    async def test_download_without_attachments_is_none(self):
        a = relay_adapters.get("sms-demo")
        self.assertIsNone(await a.download({"file_id": "F"}))
        self.assertEqual(self.gw.urls("download"), [])

    async def test_download_with_attachments(self):
        self.registry = {"connectors": [{"id": "sms-demo", "relay": {
            "label": "sms", "attachments": True, "schemes": ["sms"]}}]}
        a = relay_adapters.get("sms-demo")
        self.assertEqual(await a.download({"file_id": "F"}), "eA==")
        (url, body), = self.gw.urls("download")
        self.assertTrue(url.endswith("/internal/channels/sms-demo/download"))
        self.assertEqual(body, {"file_id": "F"})


class RelayFlowTests(_Base):
    """Rows polled from the gateway run through the real relay model."""

    BINDINGS = {"sms-demo": {CHAT: {"connector": "sms-demo", "tier": "SEAL-1",
                                    "topic": "ops"}}}

    def setUp(self):
        super().setUp()
        self.stato = Path(tempfile.mkdtemp(prefix="relay-638-"))
        self.addCleanup(shutil.rmtree, self.stato, ignore_errors=True)
        self.posts: list = []
        self.turns: list = []
        self.asked: list = []
        self.vetted: set = set()

        def _gw(path, params=None):
            self.asked.append(params["uri"])
            return _Resp({"vetted": params["uri"] in self.vetted})

        async def _open(tier, name):
            return {"meta": {"participants": ["clodia"]}}

        async def _post(tier, name, author, text, kind="human", transport=None):
            self.posts.append((author, kind, transport, text))
            return {"id": "p1"}

        async def _turn(tier, name, meta, trigger_text="", **kw):
            self.turns.append(trigger_text)

        for p in (patch("server.api.observe._gw", _gw),
                  patch.object(channel_relay, "data_path", lambda _n: self.stato),
                  patch.object(channel_relay.cb, "load", lambda: self.BINDINGS),
                  patch.object(channel_relay.topics_client, "async_open_topic", _open),
                  patch.object(channel_relay.topics_client, "async_post_message", _post),
                  patch.object(channel_relay, "run_topic_turn", _turn),
                  patch.object(channel_relay, "_can_deliver", lambda a, s=None: True),
                  patch("server.api.channels._pick_responder",
                        lambda *a, **k: type("S", (), {"name": "clodia"})())):
            p.start()
            self.addCleanup(p.stop)
        channel_relay._POLL_BACKOFF.clear()

    async def test_a_vetted_sender_is_posted_as_channel(self):
        self.vetted = {"sms:+391234"}
        self.gw.rows = [dict(ROW)]
        self.assertEqual(await channel_relay.run_poll_cycle(1), 1)
        self.assertIn("sms:group-1", self.asked)      # chat asked first
        self.assertIn("sms:+391234", self.asked)
        (author, kind, transport, text), = self.posts
        self.assertEqual((author, kind, transport), ("sms-demo", "channel", "sms"))
        self.assertIn("riassumi", text)
        self.assertEqual(len(self.turns), 1)
        sent = self.gw.urls("send")
        self.assertTrue(sent and all(u.endswith("/internal/channels/sms-demo/send")
                                     for u, _b in sent))
        self.assertIn("Ricevuto", sent[0][1]["text"])

    async def test_an_unvetted_sender_is_refused_and_nothing_is_posted(self):
        self.gw.rows = [dict(ROW)]
        await channel_relay.run_poll_cycle(1)
        self.assertEqual((self.posts, self.turns), ([], []))
        (url, body), = self.gw.urls("send")
        self.assertTrue(url.endswith("/internal/channels/sms-demo/send"))
        self.assertEqual(body["text"], channel_relay._DENY)

    async def test_a_row_without_sender_uri_is_refused(self):
        self.vetted = {"sms:+391234"}
        self.gw.rows = [{k: v for k, v in ROW.items() if k != "sender_uri"}]
        await channel_relay.run_poll_cycle(1)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.gw.urls("send")[0][1]["text"], channel_relay._DENY)

    async def test_a_registry_failure_does_not_break_the_cycle(self):
        self.registry = RuntimeError("gateway down")
        self.gw.rows = [dict(ROW)]
        self.assertEqual(await channel_relay.run_poll_cycle(1), 0)
        self.assertEqual(self.gw.calls, [])


class TelegramUnchangedTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_posts_keep_their_kind_and_body(self):
        from .telegram_relay_adapter import ADAPTER
        captured = {}

        def _post(url, headers=None, json=None, timeout=None):
            captured["body"] = json
            return type("R", (), {"status_code": 200, "json": lambda self: {}})()

        with patch.object(topics_client._gw_http, "post", _post), \
             patch.object(topics_client, "_headers", lambda: {}):
            topics_client.post_message("SEAL-1", "ops", "telegram", "x",
                                       kind=ADAPTER.post_kind, **ADAPTER.post_fields())
        self.assertEqual(captured["body"], {"author": "telegram", "text": "x",
                                            "kind": "telegram", "attachments": []})


if __name__ == "__main__":
    unittest.main()
