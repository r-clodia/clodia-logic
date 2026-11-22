"""clodia-platform#502 — no answer shift after a cancelled turn.

The `ClaudeSDKClient` is long-lived: its messages are ONE buffered stream
shared by every `query()`, and `receive_response()` reads from its head up to
the first `ResultMessage`. Cancelling the task that reads it does not stop the
subprocess, which keeps answering the cancelled question. Before the fix the
next turn read that answer as its own: Q_n got A_(n-1), and the shift stayed.

`_SharedStreamClient` reproduces exactly that: each `query()` answers into a
single queue after a delay, whether anyone is reading or not.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

from . import session as S
from .test_session_recovery import _make_session, _patch_seams
from ..core.models import ClodiaStatus


def _result(*, interrupted: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype="error_during_execution" if interrupted else "success",
        duration_ms=1, duration_api_ms=1, is_error=interrupted,
        num_turns=1, session_id="s")


def _answer(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="m")


class _SharedStreamClient:
    """One stream for every query, like the real client. `delays` says how
    long the subprocess takes to answer each question."""

    def __init__(self, delays: dict[str, float], *, interrupt_works: bool = True):
        self.stream: asyncio.Queue = asyncio.Queue()
        self.delays = delays
        self.interrupt_works = interrupt_works
        self.interrupts = 0
        self._working: asyncio.Task | None = None

    async def query(self, prompt: str) -> None:
        self._working = asyncio.create_task(self._answer(prompt))

    async def _answer(self, prompt: str) -> None:
        await asyncio.sleep(self.delays.get(prompt, 0))
        await self.stream.put(_answer(f"A({prompt})"))
        await self.stream.put(_result())

    async def interrupt(self) -> None:
        self.interrupts += 1
        if not self.interrupt_works:
            await asyncio.sleep(3600)  # a wedged subprocess
        if self._working and not self._working.done():
            self._working.cancel()
            await self.stream.put(_result(interrupted=True))

    async def receive_response(self):
        while True:
            message = await self.stream.get()
            yield message
            if isinstance(message, ResultMessage):
                return


def _turn_patches(sess, recover=None):
    """The seams of a whole `send_user_message`, minus the stream logic."""
    async def _audit(_s):
        return None

    return [
        _patch_seams(),
        mock.patch.object(S, "_audit_turn_acquired", _audit),
        mock.patch.object(S, "_start_reasoning", lambda _s: None),
        mock.patch.object(S.bus, "publish", new=mock.AsyncMock()),
        mock.patch.object(S.activity_log, "append"),
        mock.patch.object(sess, "_record", new=mock.AsyncMock()),
        mock.patch.object(sess, "_publish_error", new=mock.AsyncMock()),
        mock.patch.object(sess, "_refresh_provider_env", return_value=False),
        mock.patch.object(sess, "_refresh_mcp_principal", return_value=False),
        mock.patch.object(sess, "_set_status",
                          new=mock.AsyncMock(side_effect=lambda st: setattr(sess, "status", st))),
        mock.patch.object(sess, "_recover_session",
                          new=mock.AsyncMock(side_effect=recover or (lambda: True))),
    ]


async def _interrupt_then_ask(sess) -> str:
    """Q1 is cancelled while the subprocess still works on it, then Q2."""
    first = asyncio.create_task(sess.send_user_message("Q1"))
    while sess._current_turn_task is None:
        await asyncio.sleep(0.005)
    await sess.interrupt_current_turn()   # ⏹ and the router's R9 override both land here
    note = await first
    assert note.startswith("⏹"), note
    return await sess.send_user_message("Q2")


class _Stack:
    def __init__(self, patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.__enter__()

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.__exit__(*exc)


class NoShiftAfterInterruptTests(unittest.IsolatedAsyncioTestCase):

    async def test_next_question_gets_its_own_answer(self):
        sess = _make_session()
        sess._client = client = _SharedStreamClient({"Q1": 0.1, "Q2": 0.3})
        recreated = []
        with _Stack(_turn_patches(sess, lambda: recreated.append(1) or True)):
            reply = await _interrupt_then_ask(sess)
        self.assertEqual(reply, "A(Q2)")   # before the fix: "A(Q1)"
        self.assertEqual(client.interrupts, 1)
        self.assertEqual(recreated, [])    # drained: the context is kept
        self.assertTrue(client.stream.empty())

    async def test_a_wedged_interrupt_recreates_the_client(self):
        sess = _make_session()
        stuck = _SharedStreamClient({"Q1": 0.1, "Q2": 0.3}, interrupt_works=False)
        fresh = _SharedStreamClient({"Q2": 0.01})
        sess._client = stuck
        causes = []

        def recover():
            causes.append(sess._recover_cause)
            sess._client = fresh
            return True

        with _Stack(_turn_patches(sess, recover)), \
                mock.patch.object(S, "INTERRUPT_DRAIN_S", 0.05):
            reply = await _interrupt_then_ask(sess)
        self.assertEqual(reply, "A(Q2)")
        self.assertEqual(causes, ["user_interrupt"])

    async def test_a_failed_recreation_leaves_the_session_in_error(self):
        sess = _make_session()
        sess._client = _SharedStreamClient({"Q1": 0.2}, interrupt_works=False)
        with _Stack(_turn_patches(sess, lambda: False)), \
                mock.patch.object(S, "INTERRUPT_DRAIN_S", 0.05):
            await sess._settle_after_interrupt()
        self.assertEqual(sess.status, ClodiaStatus.ERROR)

    async def test_a_watchdog_kill_does_not_drain(self):
        """The watchdog has already killed the subprocess and the `finally`
        recreates the client: nothing to drain, and nothing to wait for."""
        sess = _make_session()
        sess._client = _SharedStreamClient({"Q1": 0.2})
        with _Stack(_turn_patches(sess)), \
                mock.patch.object(sess, "_settle_after_interrupt",
                                  new=mock.AsyncMock()) as settle:
            first = asyncio.create_task(sess.send_user_message("Q1"))
            while sess._current_turn_task is None:
                await asyncio.sleep(0.005)
            sess._watchdog_fired = True
            sess._current_turn_task.cancel()
            note = await first
        self.assertTrue(note.startswith("⏱"), note)
        settle.assert_not_awaited()


class OrphanResultTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_result_before_any_event_is_discarded(self):
        """Defensive: whatever left an old result at the head of the stream,
        it is logged and skipped, and this query's answer is read."""
        sess = _make_session()
        sess._client = client = _SharedStreamClient({})
        await client.stream.put(_result(interrupted=True))   # leftover of Q1
        await client.query("Q2")
        with _patch_seams(), \
                mock.patch.object(S.bus, "publish", new=mock.AsyncMock()), \
                self.assertLogs(S.LOG, "WARNING") as logs:
            reply = await sess._collect_response()
        self.assertEqual(reply, "A(Q2)")
        self.assertIn("#502", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
