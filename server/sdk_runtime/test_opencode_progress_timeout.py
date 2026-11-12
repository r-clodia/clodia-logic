"""An opencode turn is judged on progress, not on wall-clock alone.

Owner, 12 Nov 2026: «QUESTO ERRORE VA RISOLTO UNA VOLTA PER TUTTE». A fixed
180s budget cut segretario-107 on gemma-4-26b after 3588 progress events — two
minutes streaming a long minute, then seven quick tool calls — while it was
still working. Now a turn stops when it is SILENT for too long (a hung tool
call, a model that stopped) or at a high ceiling; never because it is long and
alive.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

import httpx

from . import session as S


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.c = _Clock()
        self.b = S._TurnBudget(1200.0, idle=150.0, clock=self.c)

    def test_a_long_turn_that_keeps_progressing_is_not_stopped(self):
        for _ in range(60):                 # 10 minutes, progress every 10 s
            self.c.t += 10
            self.b.progress()
            self.assertIsNone(self.b.verdict())

    def test_silence_stops_it(self):
        self.c.t += 149
        self.assertIsNone(self.b.verdict())
        self.c.t += 1
        self.assertEqual(self.b.verdict(), "idle")

    def test_the_ceiling_stops_even_a_working_turn(self):
        while self.c.t < 1000 + 1200:
            self.c.t += 10
            self.b.progress()
        self.assertEqual(self.b.verdict(), "max")

    def test_without_the_event_stream_silence_proves_nothing(self):
        self.c.t += 600
        self.assertIsNone(self.b.verdict(watching=False))
        self.assertEqual(self.b.verdict(watching=True), "idle")


class _Hanging:
    """A client whose turn POST never answers by itself."""

    def __init__(self):
        self.gate = asyncio.Event()

    async def post(self, url, json=None, timeout=None):
        await self.gate.wait()
        return httpx.Response(200, json={"parts": []})


def _session():
    s = S.OpenCodeChatSession.__new__(S.OpenCodeChatSession)
    s._base_url = "http://oc"
    s._oc_session = "ses_1"
    s._provider_block_evt = None
    s._eventi_connesso = True
    s._eventi_generazione = 1
    return s


class PostTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_silent_turn_is_stopped_with_its_reason(self):
        s = _session()
        b = S._TurnBudget(60.0, idle=0.2)
        with mock.patch.object(S, "_OC_WATCHDOG_TICK_S", 0.05):
            with self.assertRaises(httpx.ReadTimeout):
                await s._post_turn(_Hanging(), {}, b)
        self.assertEqual(s._oc_stop_reason, "idle")

    async def test_a_progressing_turn_runs_past_the_idle_threshold(self):
        s = _session()
        b = S._TurnBudget(60.0, idle=0.2)
        s._turn_budget = b
        client = _Hanging()

        async def lavora():
            for _ in range(10):             # 0.5 s of progress, idle is 0.2 s
                await asyncio.sleep(0.05)
                s._note_event_line('data: {"type": "message.part.delta"}')
            client.gate.set()

        with mock.patch.object(S, "_OC_WATCHDOG_TICK_S", 0.05):
            asyncio.ensure_future(lavora())
            r = await s._post_turn(client, {}, b)
        self.assertEqual(r.status_code, 200)

    async def test_heartbeats_are_not_progress(self):
        s = _session()
        b = S._TurnBudget(60.0, idle=0.2)
        s._turn_budget = b

        async def battito():
            while True:
                await asyncio.sleep(0.05)
                s._note_event_line('data: {"type": "server.heartbeat"}')

        t = asyncio.ensure_future(battito())
        try:
            with mock.patch.object(S, "_OC_WATCHDOG_TICK_S", 0.05):
                with self.assertRaises(httpx.ReadTimeout):
                    await s._post_turn(_Hanging(), {}, b)
        finally:
            t.cancel()
        self.assertEqual(s._oc_stop_reason, "idle")


class MessageTests(unittest.TestCase):
    def test_idle_says_it_stopped_working(self):
        m = S._oc_turn_timeout_message(tentativo="originale", atteso=400, budget=1200,
                                       model="gemma", motivo="idle", silenzio=151, idle=150)
        self.assertIn("nessun progresso", m)
        self.assertIn("151s", m)

    def test_max_says_it_was_still_working(self):
        m = S._oc_turn_timeout_message(tentativo="originale", atteso=1200, budget=1200,
                                       model="gemma", motivo="max")
        self.assertIn("lavorava ancora", m)


if __name__ == "__main__":
    unittest.main()
