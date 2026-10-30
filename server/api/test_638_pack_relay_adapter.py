"""A pack's declared relay transport reaches the room through the gateway
(clodia-platform#638).

The gateway is faked at the HTTP boundary: the registry (`GET
/internal/connectors`) and the channel routes (`POST
/internal/channels/<id>/{poll,send,download}`). The relay model is the real one.
"""
from __future__ import annotations

import asyncio
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


def _get(connector):
    """What `run_poll_cycle` does: refresh the projection (TTL-cached), then
    look the adapter up. `relay_adapters.get` alone never fetches."""
    pack_relay_adapter.refresh()
    return relay_adapters.get(connector)


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
        self.cursors: list = []      # cursor returned by each poll, in order

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append((url, json))
        if url.endswith("/poll"):
            rows, self.rows = self.rows, []
            out = {"messages": rows}
            if self.cursors:
                out["cursor"] = self.cursors.pop(0)
            return _Resp(out)
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
        a = _get("sms-demo")
        self.assertIsInstance(a, pack_relay_adapter.GatewayChannelAdapter)
        self.assertEqual((a.connector, a.transport_label, a.uri_scheme, a.post_kind),
                         ("sms-demo", "sms", "sms", "channel"))
        self.assertEqual(a.post_fields(), {"transport": "sms"})

    def test_a_connector_without_relay_has_none(self):
        self.assertIsNone(_get("mail"))

    def test_get_never_fetches_the_registry(self):
        """`get` runs on the event loop (`_relay_chat`): cache only."""
        self.assertIsNone(relay_adapters.get("sms-demo"))
        self.assertEqual(self.registry_calls, 0)
        pack_relay_adapter.refresh()
        self.assertIsNotNone(relay_adapters.get("sms-demo"))
        self.assertEqual(self.registry_calls, 1)

    def test_the_registry_is_cached(self):
        _get("sms-demo")
        _get("sms-demo")
        _get("unknown")
        self.assertEqual(self.registry_calls, 1)

    def test_telegram_keeps_its_builtin_adapter(self):
        from .telegram_relay_adapter import ADAPTER
        self.registry = {"connectors": [
            {"id": "telegram", "relay": {"label": "fake", "schemes": ["tg"]}}]}
        self.assertIs(_get("telegram"), ADAPTER)
        self.assertEqual(ADAPTER.post_kind, "telegram")
        self.assertEqual(ADAPTER.post_fields(), {})

    def test_a_registry_failure_is_not_fatal_and_keeps_telegram(self):
        from .telegram_relay_adapter import ADAPTER
        self.registry = RuntimeError("gateway down")
        self.assertIsNone(_get("sms-demo"))
        self.assertIs(_get("telegram"), ADAPTER)

    def test_a_registry_failure_keeps_the_last_known_adapters(self):
        a = _get("sms-demo")
        self.registry = RuntimeError("gateway down")
        pack_relay_adapter.refresh(force=True)
        self.assertIs(_get("sms-demo"), a)

    def test_a_removed_pack_loses_its_adapter(self):
        self.assertIsNotNone(_get("sms-demo"))
        self.registry = {"connectors": []}
        pack_relay_adapter.refresh(force=True)
        self.assertIsNone(_get("sms-demo"))


class AdapterTests(_Base):
    async def test_send_goes_to_the_gateway_channel_route(self):
        a = _get("sms-demo")
        await a.send(CHAT, "ciao", thread_id=7)
        (url, body), = self.gw.urls("send")
        self.assertTrue(url.endswith("/internal/channels/sms-demo/send"), url)
        self.assertEqual(body, {"chat_id": CHAT, "text": "ciao", "thread_id": 7})

    async def test_poll_remembers_the_chat_uri_and_uses_the_row_uris(self):
        a = _get("sms-demo")
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
        a = _get("sms-demo")
        self.assertIsNone(await a.download({"file_id": "F"}))
        self.assertEqual(self.gw.urls("download"), [])

    async def test_download_with_attachments(self):
        self.registry = {"connectors": [{"id": "sms-demo", "relay": {
            "label": "sms", "attachments": True, "schemes": ["sms"]}}]}
        a = _get("sms-demo")
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


class MentionsAndLabelsTests(_Base):
    """S3: text and names from the transport summon nobody in the room."""

    def test_every_at_word_of_the_text_is_neutralised(self):
        a = _get("sms-demo")
        out = a.neutralise_mentions("@clodia chiedi a @segretario, mail a x@y.it")
        self.assertEqual(out, "`@clodia` chiedi a `@segretario`, mail a x@y.it")

    def test_labels_are_sanitised(self):
        self.assertEqual(relay_adapters.safe_label("Ops @segretario\n`x`"),
                         "Ops `@segretario` 'x'")
        self.assertEqual(relay_adapters.safe_label(""), "?")
        self.assertLessEqual(len(relay_adapters.safe_label("a" * 500)), 80)

    def _request(self, adapter, row):
        from . import mentions
        out = channel_relay._request_message("clodia", row, CHAT, adapter)
        return out, mentions.extract_tags(out)

    def test_a_hostile_push_name_summons_nobody(self):
        a = _get("sms-demo")
        row = dict(ROW, text="ciao @segretario", **{"from": "@segretario boss"},
                   chat_title="g @janitor")
        out, tags = self._request(a, row)
        self.assertEqual(tags, ["clodia"])
        self.assertNotIn("\n", out)

    def test_telegram_labels_are_sanitised_too(self):
        from .telegram_relay_adapter import ADAPTER
        row = {"message_id": 1, "chat_id": "-1", "text": "@clodia x",
               "from_username": "ada", "chat_title": "Team @segretario"}
        out, tags = self._request(ADAPTER, row)
        self.assertEqual(tags, ["clodia"])
        self.assertIn("«Team `@segretario`»", out)
        # a plain name renders as before
        out, _t = self._request(ADAPTER, dict(row, chat_title="Clodia Sviluppo"))
        self.assertIn("«Clodia Sviluppo»", out)
        self.assertIn("utente telegram ada:", out)

    PROBES = ("``@clodia ciao", "`a`@clodia ciao", "a``@clodia ciao")

    def _probe(self, adapter):
        from . import mentions
        for probe in self.PROBES:
            row = {"message_id": 1, "chat_id": CHAT, "text": probe,
                   "from_username": "ada", "chat_title": "g"}
            out = channel_relay._request_message("segretario", row, CHAT, adapter)
            with self.subTest(probe=probe):
                self.assertEqual(mentions.extract_tags(out), ["segretario"])
                self.assertIn("`@clodia`", out)

    def test_backticks_cannot_unmask_a_mention_generic(self):
        self._probe(_get("sms-demo"))

    def test_backticks_cannot_unmask_a_mention_telegram(self):
        from .telegram_relay_adapter import ADAPTER
        self._probe(ADAPTER)

    def test_the_directive_is_scheme_neutral(self):
        d = channel_relay._directive(_get("sms-demo"))
        self.assertIn("sms:<chat_id>", d)
        self.assertNotIn("numerico", d)


class ActivationTests(_Base):
    """S4: what only the transport can tell about being addressed."""

    def setUp(self):
        super().setUp()
        self.a = _get("sms-demo")

    def test_a_private_chat_is_addressed(self):
        self.assertTrue(self.a.addresses_bot(
            {"text": "ciao", "chat_type": "private"}, []))

    def test_a_native_mention_is_addressed(self):
        self.assertTrue(self.a.addresses_bot(
            {"text": "ciao", "mentions_bot": True}, []))

    def test_group_chatter_is_not(self):
        self.assertFalse(self.a.addresses_bot(
            {"text": "ciao", "chat_type": "group", "mentions_bot": False}, []))
        self.assertFalse(self.a.addresses_bot({"text": "ciao", "mentions_bot": "yes"}, []))

    def test_telegram_activation_is_unchanged(self):
        from .telegram_relay_adapter import ADAPTER
        self.assertFalse(ADAPTER.addresses_bot(
            {"text": "ciao", "chat_type": "private", "mentions_bot": True}, []))


class AckTests(_Base):
    """Follow-up 1: the previous batch is acked only once handed over."""

    def _polls(self):
        return [b for _u, b in self.gw.urls("poll")]

    async def test_the_cursor_is_acked_on_the_next_poll_after_handling(self):
        a = _get("sms-demo")
        self.gw.cursors = ["c1", "c2"]
        await a.poll(1)
        await a.poll(1)              # batch c1 not handed over: no ack
        a.batch_handled()
        await a.poll(1)
        self.assertEqual(self._polls(), [{"timeout": 1}, {"timeout": 1},
                                         {"timeout": 1, "ack": "c2"}])

    async def test_no_cursor_no_ack(self):
        a = _get("sms-demo")
        await a.poll(1)
        a.batch_handled()
        await a.poll(1)
        self.assertEqual(self._polls(), [{"timeout": 1}, {"timeout": 1}])

    async def test_a_failed_poll_keeps_the_ack(self):
        a = _get("sms-demo")
        self.gw.cursors = ["c1"]
        await a.poll(1)
        a.batch_handled()

        def _down(*_a, **_k):
            raise RuntimeError("down")
        with patch.object(pack_relay_adapter.requests, "post", _down):
            with self.assertRaises(RuntimeError):
                await a.poll(1)
        await a.poll(1)
        self.assertEqual(self._polls()[-1], {"timeout": 1, "ack": "c1"})


class _Slow(relay_adapters.RelayAdapter):
    connector = "slowphone"
    transport_label = "slow"
    uri_scheme = "sl"

    def __init__(self, gate, fail=False):
        self.gate, self.fail, self.handled = gate, fail, 0

    async def poll(self, timeout):
        if self.fail:
            raise RuntimeError("transport down")
        await asyncio.wait_for(self.gate.wait(), 2)   # sequential → times out
        return []

    def batch_handled(self):
        self.handled += 1


class _Fast(_Slow):
    connector = "fastphone"

    async def poll(self, timeout):
        self.gate.set()
        return [{"chat_id": "f1", "message_id": 1, "text": "x"}]


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """Follow-up 2: one task per transport, failures isolated."""

    BINDINGS = {"slowphone": {"s1": {"tier": "SEAL-1", "topic": "a"}},
                "fastphone": {"f1": {"tier": "SEAL-1", "topic": "b"}}}

    async def _cycle(self, slow, fast):
        relayed = []

        async def _relay(chat_id, b, msgs, adapter):
            relayed.append((adapter.connector, chat_id))
        for a in (slow, fast):
            relay_adapters.register(a)
            self.addCleanup(relay_adapters.forget, a.connector)
        channel_relay._POLL_BACKOFF.clear()
        self.addCleanup(channel_relay._POLL_BACKOFF.clear)
        with patch.object(channel_relay.cb, "load", lambda: self.BINDINGS), \
             patch.object(channel_relay, "_relay_chat", _relay):
            n = await channel_relay.run_poll_cycle(1)
        return n, relayed

    async def test_transports_are_polled_concurrently(self):
        gate = asyncio.Event()
        slow, fast = _Slow(gate), _Fast(gate)
        n, relayed = await self._cycle(slow, fast)
        self.assertEqual(n, 1)
        self.assertEqual(relayed, [("fastphone", "f1")])
        self.assertEqual((slow.handled, fast.handled), (1, 1))
        self.assertNotIn("slowphone", channel_relay._POLL_BACKOFF)

    async def test_a_failing_transport_does_not_stop_the_others(self):
        gate = asyncio.Event()
        slow, fast = _Slow(gate, fail=True), _Fast(gate)
        n, relayed = await self._cycle(slow, fast)
        self.assertEqual(relayed, [("fastphone", "f1")])
        self.assertIn("slowphone", channel_relay._POLL_BACKOFF)
        self.assertEqual(slow.handled, 0)       # nothing handed over → no ack


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
