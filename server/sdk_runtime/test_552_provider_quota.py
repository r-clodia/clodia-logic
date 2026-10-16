"""clodia-platform#552 — a provider quota stops the opencode turn at once.

Terra, 15 Oct 2026: Scaleway answered `429 INSUFFICIENT QUOTA` to every call of
glm-5.2; opencode retried in silence, the turn waited out its 180 s budget and
the room read a timeout that spoke of the model and of the gateway. The only
place where opencode says why it is retrying is the `session.status` event of
type `retry`, so the turn now listens for it.

Fixed here: a quota message stops the turn on the first retry; a plain rate
limit is tolerated for a few attempts; other retries are opencode's business;
another session's events are ignored; the error is not one that buys an
automatic re-send.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from . import session as S
from .test_423_budget_turno import APPESO, _Serve, _sessione

QUOTA = ('{"status":429,"error":"INSUFFICIENT QUOTA","message":"You exceeded your '
         'current organization quota. Slow down your usage or increase your quotas."}')


def _riga(status: dict, sid: str = "oc-1") -> str:
    return "data: " + json.dumps({"type": "session.status",
                                  "properties": {"sessionID": sid, "status": status}})


def _retry(msg: str, attempt: int = 1) -> dict:
    return {"type": "retry", "attempt": attempt, "message": msg, "next": 0}


class ClassifierTests(unittest.TestCase):
    def test_quota_stops_on_the_first_retry(self):
        self.assertIsNotNone(S._oc_retry_block(_retry(QUOTA, 1)))

    def test_rate_limit_is_tolerated_then_stops(self):
        msg = "Rate limit exceeded, too many requests"
        self.assertIsNone(S._oc_retry_block(_retry(msg, 1)))
        self.assertIsNone(S._oc_retry_block(_retry(msg, 2)))
        self.assertIsNotNone(S._oc_retry_block(_retry(msg, S._OC_RATE_LIMIT_MAX_RETRIES)))

    def test_other_retries_and_statuses_are_left_to_opencode(self):
        self.assertIsNone(S._oc_retry_block(_retry("Connection reset by peer", 5)))
        self.assertIsNone(S._oc_retry_block({"type": "busy"}))
        self.assertIsNone(S._oc_retry_block({"type": "idle"}))
        self.assertIsNone(S._oc_retry_block("garbage"))

    def test_the_action_text_counts_too(self):
        st = _retry("Provider error", 1)
        st["action"] = {"title": "Quota exceeded", "message": "", "reason": "",
                        "provider": "scaleway", "label": ""}
        self.assertIsNotNone(S._oc_retry_block(st))

    def test_not_retried_automatically(self):
        from server.api.channels import _turno_ritentabile
        self.assertFalse(_turno_ritentabile(S.OpenCodeProviderLimit("x", eventi=0)))
        self.assertTrue(S.OpenCodeProviderLimit("q").nota_utente.endswith("q"))


class EventLineTests(unittest.TestCase):
    def _s(self):
        s = _sessione(1)
        s._provider_block = None
        s._provider_block_evt = asyncio.Event()
        return s

    def test_quota_on_this_session_sets_the_block(self):
        s = self._s()
        s._note_event_line(_riga(_retry(QUOTA)))
        self.assertTrue(s._provider_block_evt.is_set())
        self.assertIn("INSUFFICIENT QUOTA", s._provider_block)

    def test_another_session_is_ignored(self):
        s = self._s()
        s._note_event_line(_riga(_retry(QUOTA), sid="oc-other"))
        self.assertFalse(s._provider_block_evt.is_set())


class TurnStopsEarlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_hung_post_is_cut_short_by_the_quota_event(self):
        srv = _Serve([APPESO, (0.0, 200, "true")])   # message hangs; abort answers
        await srv.start()
        self.addAsyncCleanup(srv.stop)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sess = _sessione(srv.port)

        async def annuncia():
            await asyncio.sleep(0.3)
            sess._note_event_line(_riga(_retry(QUOTA)))

        t0 = asyncio.get_running_loop().time()
        with mock.patch.object(S, "_OPENCODE_TURN_TIMEOUT", 10.0), \
             mock.patch.object(S, "_resolve_sessions_dir",
                               return_value=pathlib.Path(d.name)), \
             mock.patch.object(S.activity_log, "append"):
            asyncio.ensure_future(annuncia())
            with self.assertRaises(S.OpenCodeProviderLimit) as ctx:
                await sess._run_turn("scarica la posta di earnext")
        secondi = asyncio.get_running_loop().time() - t0
        self.assertLess(secondi, 3.0, f"il turno ha atteso {secondi:.1f}s")
        self.assertIn("quota", str(ctx.exception))
        self.assertIn("scaleway", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, S.OpenCodeTurnTimeout)

    async def test_a_previous_turn_block_does_not_leak(self):
        srv = _Serve([(0.0, 200, '{"info":{},"parts":[]}')])
        await srv.start()
        self.addAsyncCleanup(srv.stop)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sess = _sessione(srv.port)
        sess._provider_block = "vecchio"
        with mock.patch.object(S, "_resolve_sessions_dir",
                               return_value=pathlib.Path(d.name)), \
             mock.patch.object(S.activity_log, "append"), \
             mock.patch.object(S.OpenCodeChatSession, "_handle_parts",
                               new=mock.AsyncMock(return_value="ok")):
            self.assertEqual(await sess._run_turn("ciao"), "ok")


if __name__ == "__main__":
    unittest.main()
