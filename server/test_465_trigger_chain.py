"""clodia-platform#465: the event that started a turn, and the turn that
delegated it.

- `turn.start.decision.trigger` carries the id of the human message, the job
  and its run, or the Telegram message relayed; `job` and `telegram_relay` are
  trigger kinds of their own.
- A delegated turn carries the delegating turn's span (`parent_span_id`) and
  trace (`decision.trigger.parent.trace_id`), so that from any `tool.call`
  following trace and parent spans reaches the event that started the chain —
  here across a two-hop handoff.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from . import audit_events
from .api import channel_relay, channels
from .api.test_r3_one_mention import _Base
from .core import trace, turn_timing
from .scheduler import scheduler


class _Chat:
    kind = "clodia"
    _runtime_override: dict = {}

    def __init__(self, seed: str, reply: str = "fatto"):
        self.chat_id = f"chan:SEAL-2:titulon-tech:{seed}"
        self._reply = reply
        self._lock = asyncio.Lock()
        self._last_usage = {}

    async def send_user_message(self, _prompt):
        async with self._lock:
            turn_timing.adopt(turn_timing.claim(self.chat_id))
            await audit_events.turn_acquired(self)
            self._last_response_model = "claude-opus-5-5"
            return self._reply


class _Reported(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.reported: list[dict] = []

        async def report(ev):
            self.reported.append(ev)
            return True
        p = patch.object(audit_events, "report", report)
        p.start()
        self.addCleanup(p.stop)

    def starts(self) -> list[dict]:
        return [e for e in self.reported if e["type"] == "turn.start"]


class HandoffChainTests(_Reported):
    """Human message → clodia → fullstack-dev → reviewer: two hops."""

    CHAIN = {"clodia-1": "fullstack-dev-2", "fullstack-dev-2": "reviewer-3"}

    async def asyncSetUp(self) -> None:
        self.tasks: list[asyncio.Task] = []

        async def delegate(tier, name, from_agent, reply_text, principal, hop, **_kw):
            nxt = self.CHAIN.get(from_agent)
            if not nxt:
                return

            async def dispatcher():
                # What `_start_turn` does: a new operational trace for the new
                # turn (`turn_timing.begin`), then the turn in a task of its own.
                trace.bind(trace.new_id())
                await channels._run_and_post_response(
                    tier, name, nxt, _Chat(nxt), "prompt", principal=principal,
                    hop=hop + 1, trigger={"kind": "direct", "asked_by": from_agent})
            # `_spawn_bg` = `asyncio.create_task`: the task copies this context.
            self.tasks.append(asyncio.create_task(dispatcher()))

        async def noop(*_a, **_kw):
            return None
        for p in (patch.object(channels.topics_client, "async_list_messages",
                               AsyncMock(return_value=[])),
                  patch.object(channels.topics_client, "async_post_message",
                               AsyncMock(return_value={"id": "reply"})),
                  patch.object(channels, "_maybe_delegate", delegate),
                  patch.object(channels, "_typing", noop),
                  patch.object(channels, "_channel_message", noop),
                  patch.object(channels, "_spawn_bg", lambda c: c.close()),
                  patch.object(audit_events, "model_block", lambda chat, tier: {})):
            p.start()
            self.addCleanup(p.stop)

    async def _run_chain(self) -> dict[str, dict]:
        with audit_events.caused_by({"kind": "message", "message_id": "m-1"}):
            await channels._run_and_post_response(
                "SEAL-2", "titulon-tech", "clodia-1", _Chat("clodia-1"), "prompt",
                principal="davide", trigger={"kind": "direct", "asked_by": "davide"})
        while self.tasks:
            await self.tasks.pop(0)
        return {e["agent"]["spawn"]: e for e in self.starts()}

    async def test_each_delegated_turn_hangs_under_the_delegating_one(self) -> None:
        s = await self._run_chain()
        a, b, c = s["clodia-1"], s["fullstack-dev-2"], s["reviewer-3"]
        self.assertNotIn("parent_span_id", a)
        self.assertEqual(a["decision"]["trigger"]["message_id"], "m-1")
        self.assertEqual(b["parent_span_id"], a["span_id"])
        self.assertEqual(b["decision"]["trigger"]["parent"],
                         {"trace_id": a["trace_id"], "span_id": a["span_id"],
                          "spawn": "clodia-1"})
        self.assertEqual(c["parent_span_id"], b["span_id"])
        self.assertEqual(c["decision"]["trigger"]["parent"]["trace_id"], b["trace_id"])
        # Three turns, three traces (one per turn, #455) linked by parent spans.
        self.assertEqual(len({a["trace_id"], b["trace_id"], c["trace_id"]}), 3)
        for ev in (a, b, c):
            self.assertRegex(ev["trace_id"], r"^[0-9a-f]{32}$")
            self.assertRegex(ev["span_id"], r"^[0-9a-f]{16}$")
        # Every turn of the chain also names the root directly.
        self.assertEqual(c["decision"]["trigger"]["root"],
                         {"kind": "message", "message_id": "m-1"})

    async def test_from_a_tool_call_the_walk_reaches_the_human_message(self) -> None:
        s = await self._run_chain()
        c = s["reviewer-3"]
        # A verb call of the last turn, as the gateway records it (#433).
        call = {"type": "tool.call", "trace_id": c["trace_id"], "parent_span_id": c["span_id"]}
        by_span = {(e["trace_id"], e["span_id"]): e for e in self.starts()}
        node = by_span[(call["trace_id"], call["parent_span_id"])]
        hops = 0
        while "parent_span_id" in node:
            p = node["decision"]["trigger"]["parent"]
            self.assertEqual(p["span_id"], node["parent_span_id"])
            node = by_span[(p["trace_id"], p["span_id"])]
            hops += 1
        self.assertEqual(hops, 2)
        self.assertEqual(node["agent"]["spawn"], "clodia-1")
        self.assertEqual(node["decision"]["trigger"]["message_id"], "m-1")

    async def test_the_chain_does_not_leak_into_the_callers_context(self) -> None:
        await self._run_chain()
        self.assertIsNone(audit_events.cause())


class TheRealDispatcherCopiesTheContextTests(unittest.TestCase):
    def test_start_turn_spawns_the_turn_in_a_task_that_copies_the_context(self) -> None:
        """The chain above uses a stand-in dispatcher; this pins that the real
        one starts the delegate the same way (a task, which copies the chain)."""
        self.assertIn("_spawn_bg(_run_then_unclaim(", inspect.getsource(channels._start_turn))
        self.assertIn("asyncio.create_task(", inspect.getsource(channels._spawn_bg))


class RootEventTests(_Reported):
    async def test_a_job_turn_names_the_job_and_the_run(self) -> None:
        with audit_events.caused_by({"kind": "job", "job_id": 7, "run_id": "3"}):
            audit_events.message_posted("m-9")      # the job's message in the topic
            t = audit_events.Turn(tier="SEAL-1", name="ops", label="clodia-4", chat_id="c",
                                  principal="scheduler", trigger={"kind": "system"})
        await t.start()
        tr = self.starts()[0]["decision"]["trigger"]
        self.assertEqual((tr["kind"], tr["dispatch"]), ("job", "system"))
        self.assertEqual((tr["job_id"], tr["run_id"], tr["message_id"]), (7, "3", "m-9"))

    async def test_a_telegram_relay_is_its_own_kind(self) -> None:
        root = {"kind": "telegram_relay", "message_id": "m-5",
                "telegram": {"chat_id": "-100123", "message_id": "88"}}
        with audit_events.caused_by(root):
            t = audit_events.Turn(tier="SEAL-1", name="ops", label="clodia-4", chat_id="c",
                                  principal="channel", trigger={"kind": "external"})
        await t.start()
        tr = self.starts()[0]["decision"]["trigger"]
        self.assertEqual((tr["kind"], tr["dispatch"]), ("telegram_relay", "external"))
        self.assertEqual(tr["telegram"], {"chat_id": "-100123", "message_id": "88"})
        self.assertEqual(tr["message_id"], "m-5")

    async def test_a_human_message_starts_a_new_chain(self) -> None:
        with audit_events.caused_by({"kind": "message", "message_id": "old"}, parent={
                "trace_id": "a" * 32, "span_id": "b" * 16}):
            audit_events.message_posted("m-2")
            t = audit_events.Turn(tier="SEAL-1", name="ops", label="clodia-4", chat_id="c",
                                  principal="davide", trigger={"kind": "direct"})
        self.assertIsNone(t.parent)
        self.assertEqual(t.trigger["message_id"], "m-2")

    async def test_a_remote_parent_from_traceparent(self) -> None:
        parent = audit_events.parse_traceparent(f"00-{'1' * 32}-{'2' * 16}-01")
        with audit_events.caused_by(None, parent=parent):
            t = audit_events.Turn(tier="SEAL-1", name="ops", label="clodia-4", chat_id="c",
                                  principal="channel", trigger={"kind": "ai"})
        await t.start()
        ev = self.starts()[0]
        self.assertEqual(ev["parent_span_id"], "2" * 16)
        self.assertEqual(ev["decision"]["trigger"]["parent"]["trace_id"], "1" * 32)

    def test_traceparent_is_parsed_strictly(self) -> None:
        ok = f"00-{'ab' * 16}-{'cd' * 8}-01"
        self.assertEqual(audit_events.parse_traceparent(ok),
                         {"trace_id": "ab" * 16, "span_id": "cd" * 8})
        for bad in (None, "", "garbage", f"ff-{'ab' * 16}-{'cd' * 8}-01",
                    f"00-{'0' * 32}-{'cd' * 8}-01", f"00-{'ab' * 16}-{'0' * 16}-01",
                    f"00-{'ab' * 15}-{'cd' * 8}-01", f"00-{'zz' * 16}-{'cd' * 8}-01"):
            self.assertIsNone(audit_events.parse_traceparent(bad), bad)

    def test_the_parent_span_is_honoured_only_from_an_authenticated_caller(self) -> None:
        req = MagicMock()
        req.headers = {"x-orchestrator-secret": "s"}
        with patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": ""}):
            self.assertFalse(channels._paired_gateway_ok(req))   # no fail-open
        with patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": "s"}):
            self.assertTrue(channels._paired_gateway_ok(req))
        with patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": "t"}):
            self.assertFalse(channels._paired_gateway_ok(req))
        src = inspect.getsource(channels.channel_trigger_internal)
        self.assertIn("if _paired_gateway_ok(request):", src)


class TriggerInternalParentTests(unittest.IsolatedAsyncioTestCase):
    """A turn asked for through the gateway (`trigger/internal`) hangs under the
    verb call's turn when the gateway forwards it as W3C `traceparent`."""

    TP = f"00-{'1' * 32}-{'2' * 16}-01"

    async def _trigger(self, firmato, gateway_secret=None):
        channels._TRIGGERED.clear()
        self.addCleanup(channels._TRIGGERED.clear)
        seen = {}

        def fake_turn(tier, name, meta, **kw):
            seen["cause"] = audit_events.cause()

            async def _noop():
                return ("clodia", "ok")
            return _noop()

        async def body():
            return {"text": "@clodia fai una cosa", "by": "fullstack-dev"}
        req = type("R", (), {})()
        req.json, req.headers = body, {"traceparent": self.TP}
        if gateway_secret:
            req.headers["x-orchestrator-secret"] = gateway_secret
        meta = {"owner": "davide", "participants": ["fullstack-dev", "clodia"], "tier": "SEAL-1"}
        with patch.object(channels.topics_client, "open_topic", return_value={"meta": meta}), \
                patch.object(channels, "_signed_actor", return_value=firmato), \
                patch.object(channels, "_spawn_bg", side_effect=lambda c: c.close()), \
                patch.object(channels, "run_topic_turn", new=fake_turn), \
                patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": "gw-secret"}):
            await channels.channel_trigger_internal("SEAL-1", "ch", req)
        return seen["cause"]

    async def test_the_paired_gateway_links_the_turn_to_the_calling_span(self) -> None:
        c = await self._trigger(None, gateway_secret="gw-secret")
        self.assertEqual(c["parent"], {"trace_id": "1" * 32, "span_id": "2" * 16})

    async def test_a_signed_non_gateway_actor_cannot_graft_a_parent(self) -> None:
        # e.g. an external proxy with its own certificate
        c = await self._trigger("fullstack-dev")
        self.assertIsNone(c["parent"])
        self.assertEqual(c["root"], {"kind": "internal_trigger"})

    async def test_a_wrong_gateway_secret_cannot_graft_a_parent(self) -> None:
        c = await self._trigger("fullstack-dev", gateway_secret="guess")
        self.assertIsNone(c["parent"])

    async def test_an_anonymous_caller_cannot_graft_a_parent(self) -> None:
        c = await self._trigger(None)
        self.assertIsNone(c["parent"])
        self.assertEqual(c["root"], {"kind": "internal_trigger"})


class HumanMessageRootTests(_Base):
    async def test_the_posted_message_is_the_root_of_the_turns_it_starts(self) -> None:
        seen = []

        async def start(*_a, **_kw):
            seen.append(audit_events.cause())
            return True
        _posts, apri = self._channel(["owner", "worker"])
        with apri(patch.object(channels, "_start_turn", start)):
            await channels.post_channel_message("P0", "ops", "@worker guarda", "owner")
        self.assertEqual(seen[0]["root"], {"kind": "message", "message_id": "1"})
        self.assertIsNone(seen[0]["parent"])
        self.assertIsNone(audit_events.cause())       # restored after the post


class JobRootTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_topic_trigger_fire_names_the_job_and_the_run(self) -> None:
        seen = {}

        async def post(*_a, **_kw):
            seen["cause"] = audit_events.cause()
            return {"responder": "clodia"}
        job = {"id": 7, "topic_tier": "SEAL-1", "topic_name": "ops", "prompt": "check",
               "agent": "", "run_seq": 2}
        with patch.object(channels, "post_channel_message", post), \
                patch.object(scheduler.db, "get_job", return_value=job), \
                patch.object(scheduler.db, "mark_run", return_value="3"), \
                patch.object(scheduler.db, "count_fire", return_value=None):
            await scheduler._fire_topic_trigger(job)
        self.assertEqual(seen["cause"]["root"], {"kind": "job", "job_id": 7, "run_id": "3"})

    async def test_an_agentic_job_run_is_a_turn_on_the_trail(self) -> None:
        reported = []

        async def report(ev):
            reported.append(ev)
            return True
        chat = _Chat("clodia-9")
        chat.chat_id = "job:7:clodia"
        with patch.object(audit_events, "report", report), \
                patch.object(audit_events, "model_block", lambda c, t: {"provider": "p"}), \
                patch.object(scheduler.db, "complete_run", MagicMock()), \
                patch.object(scheduler.run_status, "take", return_value=("success", None)):
            await scheduler._complete_agentic_run(7, "4", chat, "digest")
        start, end = reported
        self.assertEqual((start["type"], end["type"]), ("turn.start", "turn.end"))
        tr = start["decision"]["trigger"]
        self.assertEqual((tr["kind"], tr["job_id"], tr["run_id"]), ("job", 7, "4"))
        self.assertEqual(start["trace_id"], end["trace_id"])
        self.assertEqual(end["result"]["status"], "ok")


class TelegramRootTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_relay_names_the_telegram_message_and_the_channel_message(self) -> None:
        seen = {}

        async def turn(*_a, **_kw):
            seen["cause"] = audit_events.cause()
        with patch.object(channel_relay.topics_client, "async_post_message",
                          AsyncMock(return_value={"id": "m-77"})), \
                patch.object(channel_relay, "run_topic_turn", turn), \
                patch.object(channels, "_pick_responder", return_value=None):
            await channel_relay._act_on_telegram_request(
                -100123, "messaggero", "SEAL-1", "ops", {"tier": "SEAL-1"},
                ["clodia", "messaggero"],
                {"message_id": 88, "text": "@clodia ciao", "from_username": "davide"})
        self.assertEqual(seen["cause"]["root"], {
            "kind": "telegram_relay", "message_id": "m-77",
            "telegram": {"chat_id": "-100123", "message_id": "88"}})


if __name__ == "__main__":
    unittest.main()
