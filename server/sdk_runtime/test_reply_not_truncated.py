"""La risposta nel log di attività non si tronca a 160 caratteri (#208).

Misura della issue: un run fallito ha registrato **161 caratteri** contro 1370
token di output, e la riga persistita finisce in `…` esattamente dove sarebbe
iniziata la spiegazione. I 161 sono `_snippet(text, n=160)` — 160 più i puntini.

Il troncamento aveva un secondo difetto, non nominato dalla issue: `_snippet` fa
`" ".join(text.split())`, quindi appiattisce i newline. Un rifiuto è
strutturato, e anche i 160 caratteri che sopravvivevano arrivavano schiacciati
su una riga.
"""
from __future__ import annotations

import unittest

from . import session as S


class ReplyKeepsTheDiagnosisTests(unittest.TestCase):

    def test_a_long_reply_survives_well_past_160_chars(self):
        testo = "x" * 5000
        self.assertGreaterEqual(len(S._reply_text(testo)), 4000)

    def test_a_reply_keeps_its_line_structure(self):
        rifiuto = ("Non rientra nel mio ambito.\n\n"
                   "Il mio ruolo qui è mantenere lo stato scritto del topic:\n"
                   "- non leggo il web\n"
                   "- non riassumo fonti esterne\n")
        salvato = S._reply_text(rifiuto)
        self.assertIn("\n", salvato)
        self.assertIn("- non leggo il web", salvato)

    def test_there_is_still_a_ceiling(self):
        """Non «mai troncare»: il log di attività è un JSONL che si rilegge tutto
        per la leaderboard, e una risposta da 200k dentro una riga la
        rileggerebbe ogni volta. 4k è il tetto che la issue propone."""
        fuori = S._reply_text("y" * 200_000)
        self.assertLess(len(fuori), 4200)
        self.assertTrue(fuori.endswith("…"))

    def test_previews_of_the_prompt_stay_short(self):
        """`_snippet` resta a 160 dov'è un'anteprima e non una diagnosi: le
        allargassimo tutte, il JSONL crescerebbe senza che nessuno ci guadagni."""
        self.assertLessEqual(len(S._snippet("z" * 5000)), 161)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
