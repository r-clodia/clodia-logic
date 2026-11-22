"""clodia-platform#692 — an opencode turn whose tool calls loop stops early.

17 Nov 2026, officer-28 (glm-5.2): after a large `web_fetch` the model read the
same tool-output file 345 times and edited it 166 times, until the 1200 s
ceiling. The idle watchdog (#674) cannot see that: a loop is busy, not silent.
The turn now watches its own tool calls on the opencode event stream.

Fixed here: identical calls with identical results stop the turn; so does a
short cycle of them, and the same tool hammering one target; calls with
different arguments, a result that changes, or new text in between do not;
another session's events are ignored; the stop aborts the opencode session,
lands in the activity log, names the tool for the room and is not retried.
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


def _tool(call: str, tool: str, inp: dict, output: str = "ok",
          status: str = "completed", sid: str = "oc-1") -> dict:
    return {"id": f"prt_{call}", "sessionID": sid, "type": "tool", "callID": call,
            "tool": tool, "state": {"status": status, "input": inp, "output": output}}


def _riga(part: dict) -> str:
    return "data: " + json.dumps({"type": "message.part.updated",
                                  "properties": {"part": part}})


def _det(repeats: int = 6, target: int = 0) -> S._ToolLoopDetector:
    return S._ToolLoopDetector(repeats=repeats, target_repeats=target)


class DetectorTests(unittest.TestCase):
    def test_identical_calls_are_a_loop(self):
        d = _det(repeats=5)
        verdetti = [d.feed_part(_tool(f"c{i}", "read", {"filePath": "/w/out.txt"},
                                      "same content"))
                    for i in range(5)]
        self.assertEqual(verdetti[:4], [None] * 4)
        v = verdetti[4]
        self.assertEqual((v["rule"], v["tool"], v["target"], v["count"]),
                         ("repeat", "read", "/w/out.txt", 5))

    def test_argument_spacing_and_key_order_do_not_hide_a_loop(self):
        d = _det(repeats=3)
        d.feed_call("bash", {"command": "ls  -la", "timeout": 5}, "x")
        d.feed_call("bash", {"timeout": 5, "command": "ls -la"}, "x")
        self.assertIsNotNone(d.feed_call("bash", {"command": " ls -la ", "timeout": 5}, "x"))

    def test_a_short_cycle_is_a_loop(self):
        d = _det(repeats=4)
        v = None
        for i in range(20):
            if i % 2:
                v = d.feed_call("read", {"filePath": "/w/a"}, "A")
            else:
                v = d.feed_call("grep", {"pattern": "foo"}, "no match")
            if v:
                break
        self.assertIsNotNone(v)
        self.assertLess(d.calls, 12)

    def test_different_arguments_are_not_a_loop(self):
        d = _det(repeats=3)
        for i in range(50):
            self.assertIsNone(d.feed_call("read", {"filePath": f"/w/f{i}.py"}, "x"))

    def test_a_changing_result_is_progress(self):
        # A status polled until it flips: same call, the answer moves.
        d = _det(repeats=3)
        for i in range(30):
            self.assertIsNone(d.feed_call("bash", {"command": "gh run view 1"},
                                          f"in_progress {i}"))

    def test_new_work_in_between_is_progress(self):
        d = _det(repeats=3)
        for i in range(30):
            self.assertIsNone(d.feed_call("bash", {"command": "pytest -q"}, "1 failed"))
            self.assertIsNone(d.feed_call("edit", {"filePath": "/w/m.py",
                                                   "newString": f"fix {i}"}, "ok"))

    def test_new_text_is_progress_the_same_text_is_not(self):
        d = _det(repeats=3)
        call = _tool("x", "read", {"filePath": "/w/a"}, "A")
        for i in range(10):
            d.feed_part({**call, "callID": f"c{i}"})
            self.assertIsNone(d.feed_part({"type": "text", "id": f"t{i}",
                                           "text": f"step {i} done"}))
        self.assertIsNone(d.verdict)
        d2 = _det(repeats=3)
        v = None
        for i in range(10):
            v = v or d2.feed_part({**call, "callID": f"c{i}"})
            d2.feed_part({"type": "text", "id": f"t{i}", "text": "let me read it again"})
        self.assertIsNotNone(v)

    def test_same_target_with_changing_edits_is_a_loop(self):
        # The incident: read/edit of one file, every edit different.
        d = _det(repeats=6, target=10)
        v = None
        for i in range(40):
            v = (d.feed_call("read", {"filePath": "/w/tool-output/1.txt"}, f"v{i}")
                 or d.feed_call("edit", {"filePath": "/w/tool-output/1.txt",
                                         "oldString": f"v{i}", "newString": f"v{i+1}"},
                                "ok"))
            if v:
                break
        self.assertEqual((v["rule"], v["target"], v["count"]),
                         ("target", "/w/tool-output/1.txt", 10))

    def test_a_url_target_drops_the_query_string(self):
        d = _det(repeats=0, target=2)
        d.feed_call("webfetch", {"url": "https://x.example/a?token=SECRET"}, "1")
        v = d.feed_call("webfetch", {"url": "https://x.example/a?token=SECRET&p=2"}, "2")
        self.assertEqual(v["target"], "https://x.example/a")

    def test_a_call_is_counted_once_and_only_when_over(self):
        d = _det(repeats=2)
        p = _tool("c1", "read", {"filePath": "/w/a"}, "A")
        self.assertIsNone(d.feed_part({**p, "state": {"status": "running",
                                                      "input": {"filePath": "/w/a"}}}))
        for _ in range(5):          # the same call updated again and again
            self.assertIsNone(d.feed_part(p))
        self.assertEqual(d.calls, 1)

    def test_zero_disables(self):
        d = _det(repeats=0, target=0)
        self.assertFalse(d.enabled)
        for i in range(100):
            self.assertIsNone(d.feed_call("read", {"filePath": "/w/a"}, "A"))

    def test_env_thresholds_have_sane_defaults(self):
        self.assertGreaterEqual(S._OPENCODE_LOOP_REPEATS, 3)
        self.assertGreater(S._OPENCODE_LOOP_TARGET_REPEATS, S._OPENCODE_LOOP_REPEATS)
        with mock.patch.dict("os.environ", {"X_LOOP": "nope"}):
            self.assertEqual(S._env_int("X_LOOP", 7), 7)
        with mock.patch.dict("os.environ", {"X_LOOP": "-3"}):
            self.assertEqual(S._env_int("X_LOOP", 7), 0)


class EventLineTests(unittest.TestCase):
    def _s(self):
        s = _sessione(1)
        s._tool_loop = _det(repeats=3)
        s._tool_loop_block = None
        s._tool_loop_evt = asyncio.Event()
        return s

    def test_a_loop_on_this_session_sets_the_block(self):
        s = self._s()
        for i in range(3):
            s._note_event_line(_riga(_tool(f"c{i}", "read", {"filePath": "/w/a"})))
        self.assertTrue(s._tool_loop_evt.is_set())
        self.assertEqual(s._tool_loop_block["tool"], "read")

    def test_another_session_is_ignored(self):
        s = self._s()
        for i in range(10):
            s._note_event_line(_riga(_tool(f"c{i}", "read", {"filePath": "/w/a"},
                                           sid="oc-other")))
        self.assertFalse(s._tool_loop_evt.is_set())

    def test_no_guard_outside_a_turn(self):
        s = _sessione(1)
        for i in range(10):
            self.assertTrue(s._note_event_line(
                _riga(_tool(f"c{i}", "read", {"filePath": "/w/a"}))))


class TurnStopsOnLoopTests(unittest.IsolatedAsyncioTestCase):
    async def _turno(self, script, eventi, *, attesa=0.3):
        srv = _Serve(script)
        await srv.start()
        self.addAsyncCleanup(srv.stop)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sess = _sessione(srv.port)
        sess._oc_password = "pw"      # the #706 basic auth must ride along

        async def emetti():
            await asyncio.sleep(attesa)
            for riga in eventi:
                sess._note_event_line(riga)

        log = mock.MagicMock()
        with mock.patch.object(S, "_OPENCODE_TURN_TIMEOUT", 10.0), \
             mock.patch.object(S, "_OPENCODE_LOOP_REPEATS", 4), \
             mock.patch.object(S, "_OPENCODE_LOOP_TARGET_REPEATS", 50), \
             mock.patch.object(S, "_resolve_sessions_dir",
                               return_value=pathlib.Path(d.name)), \
             mock.patch.object(S.activity_log, "append", log), \
             mock.patch.object(S.OpenCodeChatSession, "_handle_parts",
                               new=mock.AsyncMock(return_value="fatto")):
            asyncio.ensure_future(emetti())
            t0 = asyncio.get_running_loop().time()
            try:
                esito = await sess._run_turn("riassumi la pagina")
            except Exception as e:  # noqa: BLE001
                esito = e
        return esito, asyncio.get_running_loop().time() - t0, srv, log

    async def test_repeated_identical_calls_stop_the_turn_with_the_reason(self):
        eventi = [_riga(_tool(f"c{i}", "read",
                              {"filePath": "/w/.local/share/opencode/tool-output/t1"},
                              "same")) for i in range(10)]
        err, secondi, srv, log = await self._turno(
            [APPESO, (0.0, 200, "true")], eventi)
        self.assertIsInstance(err, S.OpenCodeToolLoop)
        self.assertNotIsInstance(err, S.OpenCodeTurnTimeout)
        self.assertLess(secondi, 3.0, f"il turno ha atteso {secondi:.1f}s")
        self.assertIn("ciclo", str(err))
        self.assertIn("`read`", str(err))
        self.assertIn("tool-output/t1", str(err))
        self.assertIn("4 volte", str(err))
        self.assertTrue(err.nota_utente.startswith("⛔ turno fermato"))
        self.assertEqual(err.loop["rule"], "repeat")
        # the opencode session was aborted
        self.assertIn("/session/oc-1/abort", srv.paths)
        # and the activity log knows which tool looped
        rec = [c.args[2] for c in log.call_args_list
               if c.args[1] == "error" and c.args[2].get("error") == "opencode tool loop"]
        self.assertEqual(len(rec), 1)
        self.assertEqual((rec[0]["tool"], rec[0]["count"]), ("read", 4))
        # the audit trail classifies it, and it is never re-sent on its own
        from server import audit_events
        from server.api.channels import _turno_ritentabile, _diagnosi
        self.assertEqual(audit_events.failure_class(err), "tool_loop")
        self.assertFalse(_turno_ritentabile(err))
        self.assertIn("turno fermato: ciclo", _diagnosi(err))

    async def test_legitimate_repeats_do_not_stop_the_turn(self):
        eventi = []
        for i in range(12):
            eventi.append(_riga(_tool(f"t{i}", "bash", {"command": "pytest -q"},
                                      "1 failed")))
            eventi.append(_riga(_tool(f"e{i}", "edit", {"filePath": "/w/m.py",
                                                        "newString": f"fix {i}"})))
            eventi.append(_riga(_tool(f"r{i}", "read", {"filePath": f"/w/f{i}.py"})))
        err, _s, srv, log = await self._turno(
            [(0.6, 200, '{"info":{},"parts":[]}')], eventi, attesa=0.05)
        self.assertEqual(err, "fatto")
        self.assertNotIn("/session/oc-1/abort", srv.paths)

    async def test_a_previous_turn_loop_does_not_leak(self):
        srv = _Serve([(0.0, 200, '{"info":{},"parts":[]}')])
        await srv.start()
        self.addAsyncCleanup(srv.stop)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sess = _sessione(srv.port)
        sess._tool_loop_block = {"tool": "read", "count": 99}
        with mock.patch.object(S, "_resolve_sessions_dir",
                               return_value=pathlib.Path(d.name)), \
             mock.patch.object(S.activity_log, "append"), \
             mock.patch.object(S.OpenCodeChatSession, "_handle_parts",
                               new=mock.AsyncMock(return_value="ok")):
            self.assertEqual(await sess._run_turn("ciao"), "ok")


if __name__ == "__main__":
    unittest.main()
