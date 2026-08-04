"""`$nome` è una citazione, non un'invocazione.

Sintomo: nel canale bilancio-tomato-2026 gli agenti usavano `@` quasi sempre (125
mention hard contro 16 soft su 207 messaggi). La diagnosi ovvia era «scelgono il
sigillo sbagliato», ma nel runtime i due sigilli facevano la STESSA cosa —
`$` avviava un turno come `@` — e la direttiva soft ordinava di postare un cenno
anche a chi non aveva nulla da aggiungere. Cioè: `$` costava come `@` più un
messaggio vuoto, e la distinzione che chiedevamo di usare non esisteva.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channels


class SoftAckSamplingTests(unittest.TestCase):
    """Campionamento deterministico, non un dado."""

    def test_the_same_message_always_decides_the_same_way(self):
        """Un retry non deve raddoppiare il cenno, e un replay del canale deve
        ricostruire la stessa storia."""
        args = ("commercialista", "avvocato", "riprendo il punto sul bilancio")
        first = channels._soft_ack_selected(*args)
        for _ in range(20):
            self.assertIs(channels._soft_ack_selected(*args), first)

    def test_a_rate_of_zero_never_acks(self):
        with patch.dict("os.environ", {"CHANNEL_SOFT_ACK_RATE": "0"}):
            self.assertFalse(channels._soft_ack_selected("a", "b", "x"))

    def test_a_rate_of_one_always_acks(self):
        with patch.dict("os.environ", {"CHANNEL_SOFT_ACK_RATE": "1"}):
            self.assertTrue(channels._soft_ack_selected("a", "b", "x"))

    def test_the_default_rate_samples_about_a_fifth(self):
        """Non si asserisce 1/5 esatto — si asserisce che è una minoranza e non
        zero, che è la proprietà che serve. Un test sull'esattezza di un hash
        misurerebbe l'hash, non il comportamento."""
        with patch.dict("os.environ", {}, clear=True):
            hits = sum(channels._soft_ack_selected("agente", "collega", f"msg {i}")
                       for i in range(400))
        self.assertGreater(hits, 20)
        self.assertLess(hits, 160)

    def test_an_unparseable_rate_falls_back_instead_of_raising(self):
        with patch.dict("os.environ", {"CHANNEL_SOFT_ACK_RATE": "molto"}):
            self.assertAlmostEqual(channels._soft_ack_rate(), 0.2)


class SoftDirectiveTests(unittest.TestCase):
    def test_the_citation_directive_no_longer_mandates_a_nod(self):
        """Ordinare un cenno «anche se non hai nulla da aggiungere» fabbricava
        rumore per istruzione: era la riga che riempiva il canale."""
        d = channels._tag_directive("soft", "commercialista", "testo")
        self.assertIsNotNone(d)
        self.assertIn("UNA RIGA", d)
        self.assertIn("Nessun lavoro", d)
        self.assertNotIn("altrimenti posta", d)

    def test_the_direct_directive_states_the_cost_of_a_hard_mention(self):
        """La direttiva presentava `@` e `$` come un menu, senza criterio né
        costo. A quel punto `@` è la scelta razionale: è lo strumento più forte
        per «portare a casa l'obiettivo», che è ciò che le chiediamo."""
        d = channels._tag_directive("direct", "davide", "testo")
        self.assertIn("apre un turno completo", d)
        self.assertIn("non apre un turno", d)
        self.assertIn("In dubbio", d)

    def test_soft_ack_shares_the_citation_directive(self):
        self.assertEqual(channels._tag_directive("soft-ack", "x", "t"),
                         channels._tag_directive("soft", "x", "t"))


if __name__ == "__main__":
    unittest.main()
