"""Il feedback 👍/👎 sui messaggi non è più esposto — e l'altro «feedback» sì.

clodia-platform#416, richiesta dell'owner: il pollice su/giù è inefficace nella
pratica (su questa istanza non esiste nemmeno un `feedback-lessons.json`, per
nessun agente, da quando l'endpoint esiste) e va rimosso end-to-end.

Questi test sarebbero ROSSI prima della rimozione: le tre rotte c'erano.
Servono perché nel codice convivono due meccanismi chiamati «feedback» e ne va
tolto uno solo: il secondo test dice, in modo eseguibile, quale resta.
"""
from __future__ import annotations

import unittest

from . import channels


def _paths() -> set[str]:
    return {getattr(r, "path", "") for r in channels.router.routes}


class RotteRimosseTests(unittest.TestCase):
    def test_nessuna_rotta_di_feedback_sui_messaggi(self) -> None:
        residue = [p for p in _paths()
                   if p.endswith("/feedback") and "/messages/" in p]
        self.assertEqual(residue, [])

    def test_nessuna_rotta_lessons(self) -> None:
        residue = [p for p in _paths() if "feedback-lessons" in p]
        self.assertEqual(residue, [])

    def test_il_feedback_di_routing_resta(self) -> None:
        """È l'altro meccanismo: few-shot k-NN sulla scelta dell'agente da parte
        del router, fuori dall'ambito dell'issue. Toglierlo sarebbe stato il modo
        più facile di sbagliare questa rimozione."""
        self.assertIn("/clodia/routing/feedback", _paths())

    def test_le_funzioni_di_generazione_lesson_sono_sparite(self) -> None:
        for nome in ("_generate_feedback_lesson", "_vet_feedback_lesson",
                     "channel_message_feedback", "channel_feedback_lessons"):
            self.assertFalse(hasattr(channels, nome), nome)


if __name__ == "__main__":
    unittest.main()
