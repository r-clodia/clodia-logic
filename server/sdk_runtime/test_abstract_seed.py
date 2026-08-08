"""Un seed astratto non si spawna.

Specification §1.4, terza delle tre condizioni senza cui l'arciseed diventerebbe
il settimo meccanismo dichiarato che nessuno porta: **`abstract: true` va imposto
al momento dello spawn, non solo dichiarato**.

Perché imporlo e non fidarsi. Un seed astratto materializzato per errore non
esplode: è un agente con i soli verbi base — leggere e parlare — e nessun
mestiere. Funziona abbastanza da non farsene accorgere, e poi risponde male senza
che nulla lo segnali. È il modo silenzioso di sbagliare, che in questo modello è
sempre il più costoso.

Il controllo sta nel **choke point unico** della creazione, quindi vale per le
chat e per i job insieme: `fire_job` passa di lì.
"""
from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

from . import session as S
from ..agents.models import AgentSpec


class _Spec:
    def __init__(self, abstract):
        self.abstract = abstract


class RefusalTests(unittest.TestCase):
    def test_an_abstract_seed_is_refused(self):
        with patch.object(S, "_kind_spec", lambda k: _Spec(True)):
            with self.assertRaises(ValueError) as cm:
                S._refuse_if_abstract("professionista")
            self.assertIn("astratto", str(cm.exception))

    def test_an_ordinary_seed_passes(self):
        with patch.object(S, "_kind_spec", lambda k: _Spec(False)):
            S._refuse_if_abstract("avvocato")

    def test_a_seed_with_no_spec_passes(self):
        """Un kind statico non ha una spec nel registry: rifiutarlo qui
        bloccherebbe gli agenti nativi."""
        with patch.object(S, "_kind_spec", lambda k: None):
            S._refuse_if_abstract("clodia")

    def test_a_spec_without_the_field_passes(self):
        """I seed scritti prima del campo non devono diventare inspawnabili."""
        class _Vecchio:
            pass

        with patch.object(S, "_kind_spec", lambda k: _Vecchio()):
            S._refuse_if_abstract("legacy")

    def test_the_refusal_says_what_to_do_instead(self):
        """Chi ci arriva sta quasi certamente cercando un discendente: un seed
        astratto è un antenato, e la cosa che voleva spawnare ha un altro nome.
        Un rifiuto che non lo dice lascia a indovinare."""
        with patch.object(S, "_kind_spec", lambda k: _Spec(True)):
            with self.assertRaises(ValueError) as cm:
                S._refuse_if_abstract("professionista")
            t = str(cm.exception)
            self.assertIn("discende", t)
            self.assertIn("ereditato", t)


class DeclarationTests(unittest.TestCase):
    def test_the_seed_can_declare_it(self):
        spec = AgentSpec(name="professionista", display_name="Professionista",
                          description="antenato", abstract=True)
        self.assertTrue(spec.abstract)

    def test_the_default_is_concrete(self):
        """Un campo nuovo che rendesse astratto per default renderebbe
        inspawnabile ogni seed esistente al primo deploy."""
        self.assertFalse(AgentSpec(name="avvocato", display_name="Avvocato",
                                     description="mestiere").abstract)


class ChokePointTests(unittest.TestCase):
    def test_the_check_runs_in_the_single_creation_path(self):
        """Se stesse su una rotta della webui, un job lo aggirerebbe — e i job
        sono proprio il caso in cui nessuno guarda."""
        src = inspect.getsource(S.ChatManager.create)
        self.assertIn("_refuse_if_abstract", src)

    def test_it_runs_before_anything_is_allocated(self):
        """Rifiutare dopo aver creato lo scratch o consumato un ordinale
        lascerebbe dietro di sé i resti di uno spawn che non doveva esistere."""
        src = inspect.getsource(S.ChatManager.create)
        self.assertLess(src.index("_refuse_if_abstract"), src.index("_new_chat_id"))


if __name__ == "__main__":
    unittest.main()
