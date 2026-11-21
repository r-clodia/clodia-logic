"""clodia-platform#529 — a stalled loop is not the subprocess's silence.

After a blocked event loop or a suspended host (7h35 on 9 Oct, 97 min on
18 Oct) both timers of a turn — the watchdog and the wait for the next SDK
event — woke with their deadline long gone and killed a turn that had done
nothing wrong. Here the stall is real but short: a `time.sleep` ON the loop,
with thresholds scaled down to match.
"""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

from . import session as S
from .test_session_recovery import _make_session, _patch_seams
from ..core import loop_lag

STALL = 0.6


def _stall_soon(delay: float = 0.03) -> None:
    """Block the loop (the whole thread) for STALL seconds, `delay` from now."""
    asyncio.get_running_loop().call_later(delay, time.sleep, STALL)


class _SlowClient:
    """Answers after `after` seconds of wall time, then closes the turn."""

    def __init__(self, after: float):
        self.after = after

    async def receive_response(self):
        await asyncio.sleep(self.after)
        yield AssistantMessage(content=[TextBlock(text="fatto")], model="m")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                            is_error=False, num_turns=1, session_id="s")


def _scaled():
    return mock.patch.multiple(S, WATCHDOG_TICK=0.05, COLLECT_CHUNK_TIMEOUT=0.4), \
        mock.patch.object(loop_lag, "LAG_THRESHOLD", 0.2)


class TheWaitForTheNextEvent(unittest.IsolatedAsyncioTestCase):

    async def test_a_turn_survives_a_stall_longer_than_the_chunk_timeout(self):
        sess = _make_session()
        sess._client = _SlowClient(after=STALL + 0.15)
        a, b = _scaled()
        with a, b, _patch_seams(), mock.patch.object(S.bus, "publish", new=mock.AsyncMock()):
            _stall_soon()
            reply = await sess._collect_response()
        self.assertEqual(reply, "fatto")   # before the fix: TimeoutError on waking
        self.assertGreaterEqual(sess._turn_stalled, STALL - 0.2)

    async def test_real_silence_still_times_out(self):
        sess = _make_session()
        sess._client = _SlowClient(after=60)
        a, b = _scaled()
        with a, b, _patch_seams(), mock.patch.object(S.bus, "publish", new=mock.AsyncMock()):
            t0 = time.monotonic()
            with self.assertRaises(asyncio.TimeoutError):
                await sess._collect_response()
        self.assertLess(time.monotonic() - t0, 2)

    async def test_a_stall_does_not_defer_real_silence_forever(self):
        """Even across a stall the slices keep counting: silence after it
        still reaches the limit."""
        sess = _make_session()
        sess._client = _SlowClient(after=60)
        a, b = _scaled()
        with a, b, _patch_seams(), mock.patch.object(S.bus, "publish", new=mock.AsyncMock()):
            _stall_soon()
            with self.assertRaises(asyncio.TimeoutError):
                await sess._collect_response()


class TheWatchdog(unittest.IsolatedAsyncioTestCase):

    async def _run(self, turn_s: float, *, stall: bool) -> bool:
        sess = _make_session()
        sess._client_ctx = mock.AsyncMock()
        sess._client = object()
        sess._last_event_at = asyncio.get_running_loop().time()
        turn = asyncio.create_task(asyncio.sleep(turn_s))
        with mock.patch.multiple(S, WATCHDOG_TICK=0.05, WATCHDOG_SILENCE=0.3), \
                mock.patch.object(loop_lag, "LAG_THRESHOLD", 0.2):
            if stall:
                _stall_soon()
            await sess._turn_watchdog(turn)
        try:
            await turn
        except asyncio.CancelledError:
            pass
        return sess._watchdog_fired

    async def test_it_does_not_kill_a_turn_on_waking_from_a_stall(self):
        # 0.6s blocked > 0.3s of allowed silence; the turn ends 0.15s later.
        self.assertFalse(await self._run(STALL + 0.15, stall=True))

    async def test_it_still_kills_on_real_silence(self):
        self.assertTrue(await self._run(5, stall=False))


if __name__ == "__main__":
    unittest.main()
