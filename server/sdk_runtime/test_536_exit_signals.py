"""clodia-platform#536 — every signal exit is recognised, by a rule.

The table of #503 knew only SIGKILL and SIGTERM (-9/137, -15/143): a SIGABRT
or a SIGSEGV went back to the bare `repr` in the room, the symptom of #492.
The rule (negative -1…-31, or 128+N with N in 1…31) recognises all eight
test signals of #504, and no ordinary exit code.
"""
from __future__ import annotations

import unittest

from claude_agent_sdk import ProcessError

from . import session as S

#: (exit code, signal): the eight of #504.
OTTO = [(-9, 9), (137, 9), (-15, 15), (143, 15), (-6, 6), (134, 6), (-11, 11), (139, 11)]


class RuleTests(unittest.TestCase):
    def test_the_eight_signals(self):
        for code, sig in OTTO:
            with self.subTest(code=code):
                self.assertEqual(sig, S._segnale_di_kill(ProcessError("x", exit_code=code)))
                self.assertEqual(sig, S._segnale_di_kill(
                    RuntimeError(f"Command failed with exit code {code}")))

    def test_ordinary_exit_codes_are_not_signals(self):
        for code in (0, 1, 2, 126, 127, 128, 160, 255, -32, -100):
            with self.subTest(code=code):
                self.assertIsNone(S._segnale_di_kill(ProcessError("x", exit_code=code)))
        self.assertIsNone(S._segnale_da_uscita(True))
        self.assertIsNone(S._segnale_da_uscita(None))

    def test_names(self):
        self.assertEqual(S._nome_segnale(11), "SIGSEGV")
        self.assertEqual(S._nome_segnale(6), "SIGABRT")
        self.assertEqual(S._nome_segnale(9), "SIGKILL")


class RoomTextTests(unittest.TestCase):
    def _nota(self, code: int) -> str:
        return S.SessioneTerminata(ProcessError("x", exit_code=code), True,
                                   segnale=S._segnale_di_kill(ProcessError("x", exit_code=code)),
                                   eventi=0).nota_utente

    def test_a_kill_from_outside_says_so(self):
        self.assertIn("terminato dall'esterno", self._nota(-9))
        self.assertIn("SIGKILL", self._nota(-9))

    def test_a_crash_is_not_blamed_on_the_outside(self):
        nota = self._nota(139)
        self.assertIn("SIGSEGV", nota)
        self.assertIn("crash", nota)
        self.assertNotIn("dall'esterno", nota)


if __name__ == "__main__":
    unittest.main()
