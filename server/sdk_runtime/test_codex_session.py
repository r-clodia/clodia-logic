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


class CodexSandboxTests(unittest.TestCase):
    """clodia-platform#204: il sandbox di codex segue la dichiarazione del seed,
    e quando non può partire lo dice invece di sparire."""

    def _cmd(self, concessi, sandbox_ok=True, thread=None):
        sess = _make_session()
        sess._thread_id = thread
        with mock.patch.object(S, "_resolve_native_allowed", return_value=concessi), \
             mock.patch.object(S, "_resolve_native_denied", return_value=[]), \
             mock.patch.object(S, "_codex_sandbox_usable", return_value=sandbox_ok):
            return sess._codex_cmd("gpt-5.5")

    def test_a_seed_without_shell_is_no_longer_unsandboxed(self):
        """Il difetto: il bypass era incondizionato, quindi un seed che non
        concede `Bash` teneva comunque una shell senza limiti."""
        cmd = self._cmd(["Read", "WebFetch"])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertIn('sandbox_mode="read-only"', cmd)
        self.assertIn('approval_policy="never"', cmd)

    def test_a_seed_with_shell_is_confined_to_its_workspace_with_network(self):
        cmd = self._cmd(["Bash", "Agent"])
        self.assertIn('sandbox_mode="workspace-write"', cmd)
        self.assertIn("sandbox_workspace_write.network_access=true", cmd)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_a_seed_nobody_declared_keeps_todays_behaviour(self):
        """`None` non restringe: un'istanza senza registry non deve trovarsi gli
        agenti codex confinati da una dichiarazione che nessuno ha scritto."""
        self.assertIn("--dangerously-bypass-approvals-and-sandbox",
                      self._cmd(None))

    def test_the_confinement_travels_on_resumed_turns_too(self):
        """IL GIUNTO. `codex exec resume` NON accetta `--sandbox` (misurato su
        codex-cli 0.149.0), e dal secondo turno di ogni chat il comando è un
        `resume`: passarlo come flag avrebbe confinato solo il primo turno, senza
        che niente lo dicesse. Perciò viaggia come `-c`, che resume accetta."""
        cmd = self._cmd(["Read"], thread="thread-1")
        self.assertEqual(cmd[2:4], ["resume", "thread-1"])
        self.assertIn('sandbox_mode="read-only"', cmd)
        self.assertNotIn("-s", cmd)
        self.assertNotIn("--sandbox", cmd)

    def test_a_sandbox_that_cannot_start_falls_back_and_says_so(self):
        """L'altro giunto: se bwrap non parte, `sandbox_mode` non restringerebbe
        la shell — la spegnerebbe. Si torna al bypass, ma un ripiego che non
        avvisa è indistinguibile dal difetto di partenza."""
        cmd = self._cmd(["Read"], sandbox_ok=False)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertNotIn('sandbox_mode="read-only"', cmd)

    def test_the_probe_measures_and_warns_once(self):
        import subprocess
        esito = mock.Mock(returncode=1, stderr=b"bwrap: No permissions to create a new namespace")
        with mock.patch.object(S, "_CODEX_SANDBOX_OK", None), \
             mock.patch.object(subprocess, "run", return_value=esito) as run:
            with self.assertLogs("agent-server.sdk_runtime.session", level="WARNING") as log:
                self.assertFalse(S._codex_sandbox_usable())
            self.assertFalse(S._codex_sandbox_usable())   # memorizzato
            self.assertEqual(run.call_count, 1)
        self.assertTrue(any("bwrap" in r.getMessage() for r in log.records))

    def test_a_codex_that_is_not_there_is_not_a_working_sandbox(self):
        import subprocess
        with mock.patch.object(S, "_CODEX_SANDBOX_OK", None), \
             mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("codex")):
            with self.assertLogs("agent-server.sdk_runtime.session", level="WARNING"):
                self.assertFalse(S._codex_sandbox_usable())
