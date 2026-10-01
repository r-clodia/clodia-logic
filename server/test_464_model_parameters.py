"""clodia-platform#464: `turn.end.model.parameters` records the model-call
parameters the platform set — reasoning effort, thinking budget or mode and
similar — per runtime (Claude SDK, OpenCode, Codex). Known keys only, values
only, never prompts; a change of effort between two turns is visible.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, patch

from . import audit_events
from .api import channels
from .sdk_runtime import session as S

PROMPT = "You are clodia. SECRET-PROMPT-TEXT"


class ClaudeParametersTests(unittest.TestCase):
    def test_known_options_are_recorded_and_nothing_else(self) -> None:
        opts = {"model": "claude-opus-5-5", "system_prompt": PROMPT, "cwd": "/spawn",
                "effort": "high", "thinking": {"type": "enabled", "budget_tokens": 8000},
                "max_turns": 40,
                "mcp_servers": {"clodia-tools": {"headers": {"Authorization": "Bearer x"}}},
                "env": {"MAX_THINKING_TOKENS": "4000", "ANTHROPIC_API_KEY": "sk-secret",
                        "AGENT_CHAT_ID": "chan:x"}}
        p = audit_events.claude_parameters(opts)
        self.assertEqual(p, {"effort": "high",
                             "thinking": {"type": "enabled", "budget_tokens": 8000},
                             "max_turns": 40, "max_thinking_tokens": "4000"})
        dump = json.dumps(p)
        for leaked in ("SECRET-PROMPT-TEXT", "sk-secret", "Bearer", "chan:x", "/spawn"):
            self.assertNotIn(leaked, dump)

    def test_an_option_wins_over_the_env(self) -> None:
        p = audit_events.claude_parameters({"max_thinking_tokens": 2000,
                                            "env": {"MAX_THINKING_TOKENS": "9000"}})
        self.assertEqual(p, {"max_thinking_tokens": 2000})

    def test_a_non_scalar_or_oversized_value_is_not_recorded(self) -> None:
        p = audit_events.claude_parameters({"effort": "x" * 500,
                                            "fallback_model": ["a", "b"],
                                            "thinking": {"type": "adaptive", "note": PROMPT}})
        self.assertEqual(p, {"thinking": {"type": "adaptive"}})

    def test_nothing_set_is_recorded_as_defaults(self) -> None:
        chat = SimpleNamespace(_opts_kwargs={"model": "claude-opus-5-5", "system_prompt": PROMPT})
        self.assertEqual(audit_events.call_parameters(chat), {"defaults": True})


    def test_options_gone_are_unknown_not_defaults(self) -> None:
        for chat in (SimpleNamespace(_opts_kwargs=None), SimpleNamespace()):
            self.assertEqual(audit_events.call_parameters(chat), {"unknown": True})
        sess = S.CodexChatSession.__new__(S.CodexChatSession)
        sess._last_codex_cmd = []
        self.assertEqual(audit_events.call_parameters(sess), {"unknown": True})


class OpenCodeParametersTests(unittest.TestCase):
    def _configured(self, effort):
        sess = S.OpenCodeChatSession.__new__(S.OpenCodeChatSession)
        sess.kind, sess.chat_id, sess.principal = "minerva", "chan:SEAL-3:t:minerva", "davide"
        sess._runtime_override, sess._provider, sess._model = {}, None, None
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(S, "_runtime_provider", return_value="scaleway"), \
                mock.patch.object(S, "_runtime_model", return_value="glm-5.2"), \
                mock.patch.object(S, "_kind_spec",
                                  return_value=SimpleNamespace(reasoning_effort=effort)), \
                mock.patch("server.api.providers._read", return_value={"api_key": "sk-test"}), \
                mock.patch("server.api.providers.provider_extra_env", return_value={}), \
                mock.patch.object(S.pki, "mint_session_token", return_value="ckt1.test"):
            sess._write_config(Path(td))
        return sess

    def test_the_effort_written_in_opencode_json_is_the_one_recorded(self) -> None:
        self.assertEqual(audit_events.call_parameters(self._configured("high")),
                         {"reasoning_effort": "high"})

    def test_the_platform_default_for_glm_is_recorded_as_set(self) -> None:
        # glm-5.2 gets `none` from the platform when the seed says nothing: it
        # IS a parameter the platform set, so it is on the trail.
        self.assertEqual(audit_events.call_parameters(self._configured(None)),
                         {"reasoning_effort": "none"})


class CodexParametersTests(unittest.TestCase):
    def test_c_overrides_on_the_command_line(self) -> None:
        cmd = ["codex", "exec", "--model", "gpt-5.5", "-c", 'sandbox_mode="read-only"',
               "-c", 'model_reasoning_effort="medium"', "-c", "model_verbosity=low", "-"]
        self.assertEqual(audit_events.codex_parameters(cmd),
                         {"reasoning_effort": "medium", "verbosity": "low"})

    def test_the_command_of_the_last_turn_is_what_is_read(self) -> None:
        sess = S.CodexChatSession.__new__(S.CodexChatSession)
        sess._last_codex_cmd = ["codex", "exec", "-c", 'model_reasoning_effort="high"', "-"]
        self.assertEqual(audit_events.call_parameters(sess), {"reasoning_effort": "high"})
        sess._last_codex_cmd = ["codex", "exec", "-c", 'sandbox_mode="read-only"', "-"]
        self.assertEqual(audit_events.call_parameters(sess), {"defaults": True})

    def test_the_codex_runtime_keeps_the_command_of_each_turn(self) -> None:
        import inspect
        src = inspect.getsource(S.CodexChatSession._run_codex_once)
        self.assertLess(src.index("self._last_codex_cmd = list(cmd)"),
                        src.index("create_subprocess_exec"))


class _Chat:
    chat_id = "chan:SEAL-2:titulon-tech:clodia"
    kind = "clodia"
    _runtime_override = {}

    def __init__(self, effort):
        self._lock = asyncio.Lock()
        self._opts_kwargs = {"effort": effort, "system_prompt": PROMPT}
        self._last_usage = {}

    async def send_user_message(self, _prompt):
        async with self._lock:
            await audit_events.turn_acquired(self)
            self._last_response_model = "claude-opus-5-5"
            return "ok"


class TwoTurnsTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_change_of_effort_between_two_turns_is_on_the_trail(self) -> None:
        reported: list[dict] = []

        async def report(ev):
            reported.append(ev)
            return True

        async def noop(*_a, **_kw):
            return None
        real_block = audit_events.model_block
        with patch.object(audit_events, "report", report), \
                patch.object(channels.topics_client, "async_list_messages",
                             AsyncMock(return_value=[])), \
                patch.object(channels.topics_client, "async_post_message", AsyncMock()), \
                patch.object(channels, "_maybe_delegate", noop), \
                patch.object(channels, "_typing", noop), \
                patch.object(channels, "_channel_message", noop), \
                patch.object(channels, "_spawn_bg", lambda c: c.close()), \
                patch.object(S, "session_provider", lambda c: "anthropic-api"), \
                patch.object(S, "_declared_model", lambda k, o: "claude-opus-5-5"), \
                patch.object(audit_events, "model_block", real_block):
            for effort in ("low", "high"):
                await channels._run_and_post_response(
                    "SEAL-2", "titulon-tech", "clodia-320", _Chat(effort), "prompt",
                    principal="davide")
        ends = [e for e in reported if e["type"] == "turn.end"]
        self.assertEqual([e["model"]["parameters"] for e in ends],
                         [{"effort": "low"}, {"effort": "high"}])
        self.assertNotIn("SECRET-PROMPT-TEXT", json.dumps(reported))


if __name__ == "__main__":
    unittest.main()
