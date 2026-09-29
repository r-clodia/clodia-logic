"""clodia-platform#433, #434, #435, #442, #443, #450 — the agent-server side.

- #450: in gateway-minting mode every claim the local signer knows — `origin`
  and `scope_tier` included — reaches the gateway's mint request.
- #433/#442: a channel turn reports `turn.start` with a W3C trace id and the
  reason it exists (trigger kind, who asked, origin chain, router mode).
- #434/#435: `turn.end` records the provider the SESSION was created with, its
  region and SEAL against the topic tier, and the model the API RETURNED next
  to the declared one.
- #443: a failed, interrupted or watchdog-killed turn ends with a status and a
  cause class, never with the message.
"""
from __future__ import annotations

import inspect
import os
import re
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from . import audit_events
from .api import channels
from .colony import pki


class MintForwardsEveryClaimTests(unittest.TestCase):
    def test_origin_and_scope_tier_reach_the_gateway(self) -> None:
        seen = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            seen.update(json)
            r = MagicMock()
            r.status_code, r.json = 200, lambda: {"token": "ckt1.x.y"}
            return r
        pki._MINT_CACHE.clear()
        with patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": "s"}), \
                patch("httpx.post", fake_post):
            pki.mint_session_token("clodia", "clodia-7", origin=["davide", "clodia"],
                                   scope_tier="SEAL-2")
        self.assertEqual(seen["origin"], ["davide", "clodia"])
        self.assertEqual(seen["scope_tier"], "SEAL-2")

    def test_no_claim_of_the_local_signer_is_left_out_of_the_gateway_path(self) -> None:
        claims = set(inspect.signature(pki.mint_session_token).parameters) - {"agent", "ttl_seconds"}
        src = inspect.getsource(pki._mint_request)
        for c in claims:
            self.assertRegex(src, rf'"{c}"', f"claim '{c}' is not sent to the gateway")


class _Chat:
    chat_id = "chan:SEAL-2:titulon-tech:clodia"
    kind = "clodia"
    _runtime_override = {"provider": "aws-region-eu", "topic_tier": "SEAL-2"}

    def __init__(self, reply=None, exc=None):
        self._reply, self._exc = reply, exc
        self._last_usage = {"input_tokens": 10, "output_tokens": 5}

    async def send_user_message(self, _prompt):
        self._last_response_model = "claude-opus-5-5"
        if self._exc:
            raise self._exc
        return self._reply


class TurnEventsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.reported: list[dict] = []

        async def report(ev):
            self.reported.append(ev)
            return True

        async def noop(*_a, **_kw):
            return None
        self._p = [
            patch.object(audit_events, "report", report),
            patch.object(channels.topics_client, "async_list_messages", AsyncMock(return_value=[])),
            patch.object(channels.topics_client, "async_post_message", AsyncMock()),
            patch.object(channels, "_maybe_delegate", noop),
            patch.object(channels, "_typing", noop),
            patch.object(channels, "_channel_message", noop),
            patch.object(channels, "_spawn_bg", lambda c: c.close()),
            patch.object(audit_events, "model_block", lambda chat, tier: {
                "provider": "aws-region-eu", "provider_seal": "SEAL-2", "topic_tier": tier,
                "seal_ok": True, "request_name": "claude-opus-5-5",
                "response_name": chat._last_response_model}),
        ]
        for p in self._p:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._p])

    async def run_turn(self, chat, trigger=None):
        return await channels._run_and_post_response(
            "SEAL-2", "titulon-tech", "clodia-320", chat, "prompt", principal="davide",
            trigger=trigger)

    async def test_start_and_end_share_a_w3c_trace_and_carry_the_trigger(self) -> None:
        await self.run_turn(_Chat(reply="fatto"), trigger={"kind": "direct", "asked_by": "davide"})
        start, end = self.reported
        self.assertEqual((start["type"], end["type"]), ("turn.start", "turn.end"))
        self.assertRegex(start["trace_id"], r"^[0-9a-f]{32}$")
        self.assertRegex(start["span_id"], r"^[0-9a-f]{16}$")
        self.assertEqual(start["trace_id"], end["trace_id"])
        self.assertEqual(start["agent"], {"seed": "clodia", "spawn": "clodia-320"})
        self.assertEqual(start["decision"]["trigger"]["kind"], "direct")
        self.assertEqual(start["scope"], {"tier": "SEAL-2", "topic": "titulon-tech"})

    async def test_end_records_provider_seal_and_the_model_that_ran(self) -> None:
        await self.run_turn(_Chat(reply="fatto"))
        end = self.reported[-1]
        self.assertEqual(end["result"]["status"], "ok")
        self.assertEqual(end["model"]["provider"], "aws-region-eu")
        self.assertEqual(end["model"]["response_name"], "claude-opus-5-5")
        self.assertTrue(end["model"]["seal_ok"])
        self.assertEqual(end["model"]["usage"], {"input_tokens": 10, "output_tokens": 5})

    async def test_a_failed_turn_ends_with_a_cause_class_not_its_message(self) -> None:
        with patch.object(channels, "_watch_report", AsyncMock()), \
                patch.object(channels, "_announce_failure", AsyncMock()):
            await self.run_turn(_Chat(exc=RuntimeError(
                "turno opencode non concluso entro 180s (modello gemma non convergente)")))
        end = self.reported[-1]
        self.assertEqual((end["result"]["status"], end["result"]["error"]),
                         ("failed", "turn_timeout"))
        self.assertNotIn("gemma", str(end["result"]))

    async def test_a_watchdog_note_is_a_watchdog_end(self) -> None:
        await self.run_turn(_Chat(reply="⏱ Turno interrotto dal watchdog: subprocess silente. Riprova."))
        self.assertEqual(self.reported[-1]["result"]["status"], "watchdog")


class ModelBlockTests(unittest.TestCase):
    def test_the_session_provider_not_the_one_we_would_pick_now(self) -> None:
        chat = _Chat()
        chat._last_response_model = "claude-opus-5-5"
        from .api import providers as P
        from .sdk_runtime import session as S
        with patch.object(S, "session_provider", lambda c: "aws-region-eu"), \
                patch.object(S, "_declared_model", lambda k, o: "claude-opus-5-5"), \
                patch.object(P, "provider_seal", lambda p: "SEAL-2"), \
                patch.object(P, "provider_meets_tier", lambda p, t: True), \
                patch.object(P, "provider_extra_env", lambda p: {"AWS_REGION": "eu-central-1"}):
            m = audit_events.model_block(chat, "SEAL-2")
        self.assertEqual((m["provider"], m["provider_region"], m["provider_seal"]),
                         ("aws-region-eu", "eu-central-1", "SEAL-2"))
        self.assertEqual((m["request_name"], m["response_name"]),
                         ("claude-opus-5-5", "claude-opus-5-5"))


class ReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_secret_no_report_and_never_raises(self) -> None:
        with patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": ""}):
            self.assertFalse(await audit_events.report({"type": "turn.start"}))
        with patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": "s",
                                     "CLODIA_TOOLS_MCP_URL": "http://127.0.0.1:9/mcp/"}):
            self.assertFalse(await audit_events.report({"type": "turn.start"}))


if __name__ == "__main__":
    unittest.main()
