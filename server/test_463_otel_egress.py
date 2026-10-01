"""clodia-platform#463 (and #471) — the agent-server side.

- Every gateway call of a turn carries W3C `traceparent` under the turn span,
  next to `X-Clodia-Trace-Id`.
- Every runtime's env is built by one function: no orchestrator-only secret
  (#471 — codex and opencode used to inherit them), no OTel content switch, and
  the proxy labelled `<spawn>:<tag>` — the tag the gateway verifies when the
  egress proxy reports the request (same HMAC as clodia-tools
  `audit.trace.egress_tag`: the vector below is computed there).
- OTel export is off by default; on, the agent-server's `invoke_agent` span has
  the turn's trace id and span id, codex gets the turn's traceparent, claude a
  per-process trace recorded as a link on the turn. No content attribute is
  ever built.
"""
from __future__ import annotations

import asyncio
import os
import tomllib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from . import audit_events, otel_export, otel_genai as G
from .api import channels
from .core import trace, turn_timing
from .sdk_runtime import child_env
from .test_audit_turns import _Chat

#: Computed with clodia-tools `server.audit.trace.egress_tag("clodia-320", secret="test-secret")`.
GATEWAY_VECTOR = ("clodia-320", "test-secret", "92e78fd9abdf7489f41d5cfc299adc4c")
OTEL_ON = {"CLODIA_OTEL_EXPORT": "1", "CLODIA_OTEL_ENDPOINT": "http://otel-collector:4318/"}


class EgressLabelTests(unittest.TestCase):
    def test_the_tag_is_the_gateways(self) -> None:
        spawn, secret, tag = GATEWAY_VECTOR
        self.assertEqual(child_env.egress_tag(spawn, secret=secret), tag)
        self.assertIsNone(child_env.egress_tag(spawn, secret=""))
        self.assertIsNone(child_env.egress_tag("", secret=secret))
        self.assertIsNone(child_env.egress_tag("no spaces", secret=secret))

    def test_the_spawn_env_is_clean_and_labelled(self) -> None:
        base = {"CLODIA_ORCHESTRATOR_SECRET": "test-secret", "GIT_TOKEN": "ghp_x",
                "HTTPS_PROXY": "http://egress-proxy:8888", "http_proxy": "http://egress-proxy:8888",
                "NO_PROXY": "clodia-tools", "OTEL_LOG_USER_PROMPTS": "1",
                "OTEL_LOG_TOOL_CONTENT": "1", "PATH": "/bin"}
        with patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": "test-secret"}):
            env = child_env.spawn_env(base, "clodia-320")
        tag = GATEWAY_VECTOR[2]
        self.assertNotIn("CLODIA_ORCHESTRATOR_SECRET", env)
        self.assertNotIn("GIT_TOKEN", env)
        self.assertNotIn("OTEL_LOG_USER_PROMPTS", env)
        self.assertNotIn("OTEL_LOG_TOOL_CONTENT", env)
        self.assertEqual(env["HTTPS_PROXY"], f"http://clodia-320:{tag}@egress-proxy:8888")
        self.assertEqual(env["http_proxy"], f"http://clodia-320:{tag}@egress-proxy:8888")
        self.assertEqual((env["NO_PROXY"], env["PATH"]), ("clodia-tools", "/bin"))
        self.assertIn("CLODIA_ORCHESTRATOR_SECRET", base)       # the input is not touched

    def test_no_label_without_a_spawn_a_secret_or_a_proxy(self) -> None:
        base = {"HTTPS_PROXY": "http://egress-proxy:8888"}
        with patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": ""}):
            self.assertEqual(child_env.spawn_env(base, "clodia-1"), base)
        with patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": "s"}):
            self.assertEqual(child_env.spawn_env(base, None), base)
            self.assertEqual(child_env.spawn_env({"HTTPS_PROXY": ""}, "clodia-1"),
                             {"HTTPS_PROXY": ""})
            # credentials the operator put there are the operator's
            own = {"HTTPS_PROXY": "http://ops:pw@corp-proxy:3128"}
            self.assertEqual(child_env.spawn_env(own, "clodia-1"), own)


class EveryRuntimeUsesTheSpawnEnvTests(unittest.TestCase):
    """#471: one function for the three runtimes, so none inherits the secret."""

    def test_the_three_runtimes(self) -> None:
        src = (Path(__file__).parent / "sdk_runtime" / "session.py").read_text(encoding="utf-8")
        self.assertIn("child_env = spawn_env(child_env,", src)            # claude
        self.assertIn('env = spawn_env({**_inherited_spawn_env(), "CODEX_HOME"', src)  # codex
        self.assertIn("env = spawn_env(_inherited_spawn_env(),", src)                  # opencode
        self.assertNotIn("env = {**os.environ}", src)
        self.assertNotIn('env = {**os.environ, "CODEX_HOME"', src)

    def test_opencode_env_has_no_orchestrator_secret(self) -> None:
        import tempfile
        from .sdk_runtime import session as S
        from .sdk_runtime.test_opencode_config import _session
        sess = _session()
        sess._spawn = None
        with tempfile.TemporaryDirectory() as td, \
                patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": "s", "GIT_TOKEN": "g"}), \
                patch.object(S, "_runtime_provider", return_value=None), \
                patch.object(S, "_runtime_model", return_value=None), \
                patch.object(S.pki, "mint_session_token", return_value="ckt1.t"):
            env = sess._write_config(Path(td))
        self.assertNotIn("CLODIA_ORCHESTRATOR_SECRET", env)
        self.assertNotIn("GIT_TOKEN", env)


class TraceparentTests(unittest.TestCase):
    def test_only_with_a_span_of_the_bound_trace(self) -> None:
        async def go():
            trace.bind("a" * 32)
            self.assertEqual(trace.headers(), {trace.HEADER: "a" * 32})
            trace.bind_span("b" * 16)
            self.assertEqual(trace.headers()[trace.TRACEPARENT], f"00-{'a' * 32}-{'b' * 16}-01")
            trace.bind("c" * 32)                     # another trace: the span is not its
            self.assertIsNone(trace.traceparent())
            self.assertEqual(trace.headers(), {trace.HEADER: "c" * 32})
        asyncio.run(go())

    def test_own_turn_restores_the_span_too(self) -> None:
        @trace.own_turn
        async def nested():
            trace.bind("d" * 32)
            trace.bind_span("e" * 16)

        async def go():
            trace.bind("a" * 32)
            trace.bind_span("b" * 16)
            await nested()
            return trace.traceparent()
        self.assertEqual(asyncio.run(go()), f"00-{'a' * 32}-{'b' * 16}-01")


class GenAiMappingTests(unittest.TestCase):
    def test_invoke_agent_attributes_are_metadata_only(self) -> None:
        a = G.invoke_agent_attributes(
            seed="clodia", spawn="clodia-320", conversation="chan:SEAL-2:t:clodia",
            model={"provider": "aws-region-eu", "provider_seal": "SEAL-2", "topic_tier": "SEAL-2",
                   "request_name": "claude-opus-5-5", "response_name": "claude-opus-5-5",
                   "runtime": "ChatSession", "usage": {"input_tokens": 10, "output_tokens": 5}},
            status="ok", error=None)
        self.assertEqual(a[G.OPERATION_NAME], "invoke_agent")
        self.assertEqual(a[G.PROVIDER_NAME], "aws.bedrock")
        self.assertEqual((a[G.USAGE_INPUT_TOKENS], a[G.USAGE_OUTPUT_TOKENS]), (10, 5))
        self.assertFalse(set(a) & G.CONTENT_ATTRIBUTES)
        self.assertNotIn(G.ERROR_TYPE, a)
        self.assertEqual(G.provider_name("claude-pro-max"), "anthropic")
        self.assertEqual(G.provider_name("my-endpoint"), "my-endpoint")
        self.assertEqual(G.span_name("invoke_agent", "clodia"), "invoke_agent clodia")


class RuntimeWiringTests(unittest.TestCase):
    def test_off_by_default(self) -> None:
        with patch.dict(os.environ, {"CLODIA_OTEL_EXPORT": "", "CLODIA_OTEL_ENDPOINT": ""}):
            self.assertFalse(otel_export.enabled())
            self.assertEqual(otel_export.claude_env(seed="clodia", spawn="clodia-1"), ({}, None))
            self.assertEqual(otel_export.codex_config_toml(), "")
            self.assertEqual(otel_export.codex_turn_env("a" * 32, "b" * 16), {})
        with patch.dict(os.environ, {"CLODIA_OTEL_EXPORT": "1", "CLODIA_OTEL_ENDPOINT": ""}):
            self.assertFalse(otel_export.enabled())         # on without a place to send: off

    def test_claude_traces_only_and_no_content(self) -> None:
        with patch.dict(os.environ, OTEL_ON):
            env, rt = otel_export.claude_env(seed="clodia", spawn="clodia-320")
        self.assertRegex(rt, r"^[0-9a-f]{32}$")
        self.assertRegex(env["TRACEPARENT"], rf"^00-{rt}-[0-9a-f]{{16}}-01$")
        self.assertEqual((env["OTEL_TRACES_EXPORTER"], env["OTEL_METRICS_EXPORTER"],
                          env["OTEL_LOGS_EXPORTER"]), ("otlp", "none", "none"))
        self.assertEqual(env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"],
                         "http://otel-collector:4318/v1/traces")
        self.assertIn("clodia.spawn=clodia-320", env["OTEL_RESOURCE_ATTRIBUTES"])
        self.assertFalse(set(env) & set(otel_export.CONTENT_FLAGS))

    def test_codex_config_and_turn_traceparent(self) -> None:
        with patch.dict(os.environ, OTEL_ON):
            block = otel_export.codex_config_toml()
            cfg = tomllib.loads('[mcp_servers.clodia-tools]\nurl = "x"\n' + block)
            env = otel_export.codex_turn_env("a" * 32, "b" * 16)
        o = cfg["otel"]
        self.assertIs(o["log_user_prompt"], False)
        self.assertEqual((o["exporter"], o["metrics_exporter"]), ("none", "none"))
        self.assertEqual(o["trace_exporter"], {"otlp-http": {
            "endpoint": "http://otel-collector:4318/v1/traces", "protocol": "json"}})
        self.assertEqual(env, {"TRACEPARENT": f"00-{'a' * 32}-{'b' * 16}-01"})

    def test_what_each_runtime_supports_is_stated(self) -> None:
        self.assertEqual(otel_export.RUNTIMES, {"claude": "spawn-trace", "codex": "turn-trace",
                                                "opencode": "not-wired"})


class TurnSpanTests(unittest.IsolatedAsyncioTestCase):
    """The agent-server's own span and the runtime link, through a real turn."""

    def setUp(self) -> None:
        self.reported: list[dict] = []
        self.exported: list[dict] = []

        async def report(ev):
            self.reported.append(ev)
            return True

        async def export(payload):
            self.exported.append(payload)
            return True

        async def noop(*_a, **_kw):
            return None
        self._p = [
            patch.object(audit_events, "report", report),
            patch.object(otel_export, "export", export),
            patch.object(channels.topics_client, "async_list_messages", AsyncMock(return_value=[])),
            patch.object(channels.topics_client, "async_post_message", AsyncMock()),
            patch.object(channels, "_maybe_delegate", noop),
            patch.object(channels, "_typing", noop),
            patch.object(channels, "_channel_message", noop),
            patch.object(channels, "_spawn_bg", lambda c: c.close()),
            patch.object(audit_events, "model_block", lambda chat, tier: {
                "provider": "aws-region-eu", "provider_seal": "SEAL-2", "topic_tier": tier,
                "request_name": "claude-opus-5-5", "response_name": chat._last_response_model}),
        ]
        for p in self._p:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._p])

    async def run_turn(self, chat):
        async def dispatcher():
            t = turn_timing.begin("start_turn")
            t.bind(chat.chat_id)
            await channels._run_and_post_response(
                "SEAL-2", "titulon-tech", "clodia-320", chat, "p", principal="davide", timing=t)
        await asyncio.create_task(dispatcher())
        for _ in range(20):
            await asyncio.sleep(0)

    async def test_the_span_is_the_turn(self) -> None:
        chat = _Chat(reply="ok")
        chat._otel_runtime_trace = "f" * 32
        with patch.dict(os.environ, OTEL_ON):
            await self.run_turn(chat)
        start, end = self.reported
        self.assertEqual(start["links"], [{"trace_id": "f" * 32, "attributes": {
            "link.source": "runtime", "clodia.runtime": "claude"}}])
        (payload,) = self.exported
        span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        self.assertEqual((span["traceId"], span["spanId"]), (start["trace_id"], start["span_id"]))
        self.assertEqual(span["name"], "invoke_agent clodia")
        keys = {a["key"] for a in span["attributes"]}
        self.assertIn(G.PROVIDER_NAME, keys)
        self.assertFalse(keys & G.CONTENT_ATTRIBUTES)
        self.assertEqual(span["links"][0]["traceId"], "f" * 32)
        self.assertEqual(span["status"], {"code": 1})

    async def test_nothing_is_exported_when_off(self) -> None:
        with patch.dict(os.environ, {"CLODIA_OTEL_EXPORT": ""}):
            await self.run_turn(_Chat(reply="ok"))
        self.assertEqual(self.exported, [])
        self.assertNotIn("links", self.reported[0])


if __name__ == "__main__":
    unittest.main()
