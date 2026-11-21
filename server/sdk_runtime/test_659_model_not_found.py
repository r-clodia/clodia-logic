"""clodia-platform#659 — a model opencode does not know fails fast.

6 Nov and 11 Nov 2026: a new opencode session logged
`ProviderModelNotFoundError` for the Scaleway model three seconds after it
opened, and the turn then sat silent until the 180 s budget ran out — read as
a timeout. Seen on the event stream (`session.error`), the error now stops the
POST at once; the session is recreated ONE time, as for an unusable session,
and a second miss fails in seconds, naming the cause.
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

NAMED = {"name": "ProviderModelNotFoundError",
         "data": {"providerID": "scaleway", "modelID": "gemma-4-26b-a4b-it",
                  "suggestions": ["gemma-4-26b-a4b-it"]}}
UNNAMED = {"name": "UnknownError",
           "data": {"message": "ProviderModelNotFoundError: scaleway/gemma-4-26b-a4b-it"}}


def _riga(error: dict, sid: str | None = None) -> str:
    props = {"error": error}
    if sid:
        props["sessionID"] = sid
    return "data: " + json.dumps({"type": "session.error", "properties": props})


class ClassifierTests(unittest.TestCase):
    def test_both_shapes_are_recognised(self):
        self.assertIn("scaleway/gemma-4-26b-a4b-it", S._oc_model_missing({"error": NAMED}))
        self.assertIsNotNone(S._oc_model_missing({"error": UNNAMED}))

    def test_other_errors_are_not(self):
        for err in ({"name": "ProviderAuthError", "data": {"message": "401"}},
                    {"name": "MessageAbortedError", "data": {}}, "garbage", None):
            self.assertIsNone(S._oc_model_missing({"error": err}))

    def test_not_retried_automatically_and_said_plainly(self):
        from server.api.channels import _turno_ritentabile
        e = S.OpenCodeModelNotFound("x", eventi=0)
        self.assertFalse(_turno_ritentabile(e))
        self.assertTrue(e.nota_utente.startswith("⛔"))


class EventLineTests(unittest.TestCase):
    def _s(self):
        s = _sessione(1)
        s._model_missing = None
        s._provider_block_evt = asyncio.Event()
        return s

    def test_this_sessions_error_sets_the_event(self):
        s = self._s()
        s._note_event_line(_riga(NAMED, sid="oc-1"))
        self.assertTrue(s._provider_block_evt.is_set())
        self.assertIn("ProviderModelNotFoundError", s._model_missing)

    def test_another_sessions_error_is_ignored(self):
        s = self._s()
        s._note_event_line(_riga(NAMED, sid="oc-other"))
        self.assertFalse(s._provider_block_evt.is_set())


class _Turno(unittest.IsolatedAsyncioTestCase):
    async def _run(self, script, annunci, *, handle=None):
        srv = _Serve(script)
        await srv.start()
        self.addAsyncCleanup(srv.stop)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sess = _sessione(srv.port)

        async def annuncia():
            for t in annunci:
                await asyncio.sleep(t)
                sess._note_event_line(_riga(NAMED, sid=sess._oc_session))

        patches = [mock.patch.object(S, "_OPENCODE_TURN_TIMEOUT", 30.0),
                   mock.patch.object(S, "_OPENCODE_MIN_RETRY_BUDGET", 5.0),
                   mock.patch.object(S, "_resolve_sessions_dir",
                                     return_value=pathlib.Path(d.name)),
                   mock.patch.object(S.activity_log, "append")]
        if handle is not None:
            patches.append(mock.patch.object(S.OpenCodeChatSession, "_handle_parts",
                                             new=mock.AsyncMock(return_value=handle)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        t0 = asyncio.get_running_loop().time()
        asyncio.ensure_future(annuncia())
        try:
            out = await sess._run_turn("prepara il riepilogo")
        except Exception as e:  # noqa: BLE001
            out = e
        return out, asyncio.get_running_loop().time() - t0, srv, sess


class TurnTests(_Turno):
    async def test_a_new_session_recovers_the_turn(self):
        out, secondi, srv, sess = await self._run(
            [APPESO,                                  # message on oc-1 never returns
             (0.0, 200, "true"),                      # abort
             (0.0, 200, '{"id": "oc-2"}'),            # new session
             (0.0, 200, '{"info": {}, "parts": []}')],  # message on oc-2
            [0.3], handle="fatto")
        self.assertEqual(out, "fatto")
        self.assertLess(secondi, 3.0, f"il turno ha atteso {secondi:.1f}s")
        self.assertEqual(sess._oc_session, "oc-2")
        self.assertEqual(srv.paths, ["/session/oc-1/message", "/session/oc-1/abort",
                                     "/session", "/session/oc-2/message"])

    async def test_a_second_miss_fails_fast_with_the_cause(self):
        out, secondi, _srv, _sess = await self._run(
            [APPESO, (0.0, 200, "true"), (0.0, 200, '{"id": "oc-2"}'),
             APPESO, (0.0, 200, "true")],
            [0.3, 0.5])
        self.assertIsInstance(out, S.OpenCodeModelNotFound)
        self.assertNotIsInstance(out, S.OpenCodeTurnTimeout)
        self.assertIn("scaleway/gemma-4-26b-a4b-it", str(out))
        self.assertLess(secondi, 3.0, f"il turno ha atteso {secondi:.1f}s")


class CatalogLogTests(unittest.IsolatedAsyncioTestCase):
    async def _log(self, body: str, status: int = 200):
        srv = _Serve([(0.0, status, body)])
        await srv.start()
        self.addAsyncCleanup(srv.stop)
        sess = _sessione(srv.port)
        with self.assertLogs(S.LOG, "INFO") as logs:
            await sess._log_model_catalog()
        self.assertEqual(srv.paths, ["/config/providers"])
        return "\n".join(logs.output)

    async def test_the_model_is_found(self):
        out = await self._log(json.dumps({"providers": [
            {"id": "scaleway", "models": {"gemma-4-26b-a4b-it": {}, "glm-5.2": {}}}],
            "default": {}}))
        self.assertIn("presente", out)
        self.assertIn("2 modelli", out)

    async def test_the_model_is_missing(self):
        out = await self._log(json.dumps({"providers": [{"id": "scaleway", "models": {}}]}))
        self.assertIn("WARNING", out)
        self.assertIn("ASSENTE", out)

    async def test_an_error_is_logged_not_raised(self):
        out = await self._log("boom", status=500)
        self.assertIn("HTTP 500", out)


if __name__ == "__main__":
    unittest.main()
