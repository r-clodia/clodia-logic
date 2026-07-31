from __future__ import annotations

import unittest
from unittest import mock

from . import session as S
from .session import CodexChatSession


def _make_session(runtime_override: dict | None = None) -> CodexChatSession:
    sess = CodexChatSession.__new__(CodexChatSession)
    sess.chat_id = "chan:SEAL-1:test:fullstack-dev"
    sess.kind = "fullstack-dev"
    sess.principal = "davide"
    sess._runtime_override = runtime_override or {}
    sess._thread_id = None
    sess._last_usage = {}
    sess._context_tokens = 0
    return sess


class CodexCommandTests(unittest.TestCase):
    def test_stdin_is_explicit_for_new_and_resumed_turns(self):
        sess = _make_session()
        command = sess._codex_cmd("gpt-5-codex")
        self.assertEqual(command[-1], "-")
        self.assertNotIn("resume", command)

        sess._thread_id = "thread-1"
        resumed = sess._codex_cmd("gpt-5-codex")
        self.assertEqual(resumed[-1], "-")
        self.assertEqual(resumed[2:4], ["resume", "thread-1"])


class CodexFailureTests(unittest.TestCase):
    def test_structured_error_wins_over_stdin_status_line(self):
        failure = CodexChatSession._codex_failure(
            [],
            ["The 'gpt-5.6-sol' model is not supported with a ChatGPT account."],
            1,
            "Reading prompt from stdin...\n",
        )
        self.assertIn("not supported", failure)
        self.assertNotIn("Reading prompt", failure)

    def test_nonfatal_error_does_not_discard_a_completed_answer(self):
        self.assertEqual(
            CodexChatSession._codex_failure(
                ["Risposta"], ["event stream lagged"], 0, "",
            ),
            "",
        )


class CodexModelFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_seed_model_rejection_retries_with_codex_default(self):
        sess = _make_session()
        unsupported = (
            [],
            ["The 'gpt-5.6-sol' model is not supported with a ChatGPT account."],
            1,
            "Reading prompt from stdin...\n",
        )
        succeeded = (["Fatto"], [], 0, "")
        sess._run_codex_once = mock.AsyncMock(side_effect=[unsupported, succeeded])
        sess._restore_codex_thread = mock.Mock()

        with mock.patch.object(S, "_runtime_model", return_value="gpt-5.6-sol"), \
             mock.patch.object(S, "_runtime_provider", return_value="codex"), \
             mock.patch.object(S.activity_log, "append"):
            result = await sess._run_turn("ciao")

        self.assertEqual(result, "Fatto")
        self.assertEqual(
            sess._run_codex_once.await_args_list,
            [mock.call("ciao", "gpt-5.6-sol"), mock.call("ciao", None)],
        )
        sess._restore_codex_thread.assert_called_once_with(None)

    async def test_explicit_model_override_never_falls_back(self):
        sess = _make_session({"model": "gpt-5.6-sol"})
        sess._run_codex_once = mock.AsyncMock(return_value=(
            [],
            ["The 'gpt-5.6-sol' model is not supported with a ChatGPT account."],
            1,
            "Reading prompt from stdin...\n",
        ))

        with mock.patch.object(S, "_runtime_model", return_value="gpt-5.6-sol"), \
             mock.patch.object(S.activity_log, "append"):
            with self.assertRaisesRegex(RuntimeError, "not supported"):
                await sess._run_turn("ciao")

        sess._run_codex_once.assert_awaited_once_with("ciao", "gpt-5.6-sol")

    async def test_run_done_marks_codex_usage_as_delta_and_keeps_raw_cumulative(self):
        sess = _make_session()
        sess._run_codex_once = mock.AsyncMock(return_value=(["Fatto"], [], 0, ""))
        sess._last_usage = {"input_tokens": 250, "output_tokens": 25, "cache_read_input_tokens": 180}
        sess._usage_cumulative = {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 50}

        events: list[tuple[str, str, dict]] = []

        def _append(agent: str, typ: str, payload: dict, *args, **kwargs) -> None:
            events.append((agent, typ, payload))

        with mock.patch.object(S, "_runtime_model", return_value="gpt-5-codex"), \
             mock.patch.object(S, "_runtime_provider", return_value="codex"), \
             mock.patch.object(S.activity_log, "append", side_effect=_append):
            result = await sess._run_turn("ciao")

        self.assertEqual(result, "Fatto")
        run_done = [p for _agent, typ, p in events if typ == "run_done"][0]
        self.assertEqual(run_done["usage"], {
            "input_tokens": 150,
            "output_tokens": 15,
            "cache_read_input_tokens": 130,
        })
        self.assertEqual(run_done["usage_semantics"], "delta")
        self.assertEqual(run_done["usage_source"], "codex_cumulative_delta")
        self.assertEqual(run_done["usage_cumulative"], {
            "input_tokens": 250,
            "output_tokens": 25,
            "cache_read_input_tokens": 180,
        })


class CodexEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_failed_is_collected_for_retry_decision(self):
        sess = _make_session()
        sess._publish_error = mock.AsyncMock()
        errors: list[str] = []

        await sess._handle_event(
            {
                "type": "turn.failed",
                "error": {"message": "model gpt-5.6-sol is unsupported"},
            },
            [],
            errors,
        )

        self.assertEqual(errors, ["model gpt-5.6-sol is unsupported"])
        sess._publish_error.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
