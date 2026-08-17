"""Una destinazione censita è perimetro: non accende il terzo bit.

    «se la destinazione è censita in whitelist allora va considerata come parte
     del perimetro e non deve essere un segnale che fa scattare il gate o
     incrementare il trifecta»
                                                    — Davide, 17 ago 2026

La regola nasce da un caso misurato su `fullstack-dev`: le sue destinazioni
GitHub erano censite e il gate di contesto scattava comunque, perché il gateway
leggeva «già in whitelist» come «nessuno guarda, quindi chiedi». Quel lato è
corretto in clodia-tools (`_context_gate_needed`).

Questo file fissa l'altra metà — il terzo bit — dove il codice era **già**
conforme: `egress_scope` distingue `presided`/`listed` da `arbitrary`. Ma «già
conforme» non è documentato: senza un test che lo dica, la prossima riscrittura
del punteggio può tornare a contare qualunque verbo di uscita come uscita
arbitraria, e il segnale si riaccenderebbe su tutto senza che nulla protesti.
Un bit acceso sempre non discrimina, ed è l'unico lavoro che gli si chiede.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import trifecta
from .models import AgentSpec

#: Un agente con un verbo di uscita: è la condizione perché il terzo bit possa
#: accendersi, e quindi ciò che rende visibile la differenza fra confinato e no.
EGRESSO = ["email.send", "topic.files"]


def _ag(nome: str, verbi: list[str]) -> AgentSpec:
    return AgentSpec.model_validate({
        "name": nome, "description": "d", "display_name": nome, "model": "m",
        "system_prompt": "s.md", "tool_permissions": verbi, "native_tools": [],
    })


class EgressScopeTests(unittest.TestCase):
    """`egress_scope`: quando l'uscita di un agente è arbitraria."""

    def _conf(self, mode: str, scope: str) -> dict:
        return {"mode": mode, "egress": {"scope": scope}}

    def test_declared_destinations_under_gate_are_not_arbitrary(self) -> None:
        """Il caso di `fullstack-dev`: destinazioni censite, modo `gate`."""
        self.assertEqual("presided",
                         trifecta.egress_scope("dev", self._conf("gate", "narrow"), True))

    def test_declared_destinations_under_deny_are_listed(self) -> None:
        self.assertEqual("listed",
                         trifecta.egress_scope("dev", self._conf("on", "narrow"), True))

    def test_no_confinement_is_arbitrary(self) -> None:
        """Il rovescio, che deve restare: senza confinamento non c'è perimetro da
        rispettare, e leggere l'assenza di regole come una dichiarazione di
        sicurezza è la direzione d'errore che questa misura non può permettersi."""
        for mode in ("off", "report", "unknown"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    "arbitrary",
                    trifecta.egress_scope("dev", self._conf(mode, "narrow"), True))

    def test_a_star_rule_is_arbitrary_even_under_gate(self) -> None:
        """Una lista che contiene `*` è dichiarata e non vincola niente: è la
        differenza fra censire una destinazione e censire «tutte»."""
        self.assertEqual("arbitrary",
                         trifecta.egress_scope("dev", self._conf("gate", "wide"), True))

    def test_an_agent_without_egress_verbs_has_none(self) -> None:
        self.assertEqual("none",
                         trifecta.egress_scope("dev", self._conf("gate", "narrow"), False))


class TheThirdBitTests(unittest.TestCase):
    """Il bit sul canale, che è ciò che l'owner vede. Si chiama `context_profile`:
    replicare qui l'espressione booleana darebbe un test che passa sempre e
    documenta il falso."""

    def _prof(self, verbi=None):
        """Il profilo di un canale con un solo agente. Gli altri due bit sono
        tenuti spenti di proposito: qui si misura il terzo."""
        return trifecta.context_profile(
            ["dev"], specs=[_ag("dev", verbi if verbi is not None else EGRESSO)],
            tainted=False, remote_egress=False, channel_private_data=False,
        )

    def test_confined_egress_does_not_light_the_third_bit(self) -> None:
        """Destinazioni censite e modo `gate`: il caso di `fullstack-dev`."""
        with patch.object(trifecta, "egress_confinement",
                          return_value={"mode": "gate", "egress": {"scope": "narrow"}}):
            prof = self._prof()
        self.assertEqual(0, prof["bits"]["arbitrary_egress"])
        self.assertEqual(0, prof["score"])
        # La capacità resta dichiarata: i verbi ci sono, e negarlo sarebbe l'unica
        # bugia che questa misura non può permettersi.
        self.assertTrue(prof["legs"]["egress"])

    def test_without_confinement_the_bit_is_lit(self) -> None:
        """Il rovescio: senza confinamento non c'è perimetro da rispettare."""
        with patch.object(trifecta, "egress_confinement",
                          return_value={"mode": "off", "egress": {"scope": "narrow"}}):
            prof = self._prof()
        self.assertEqual(1, prof["bits"]["arbitrary_egress"])

    def test_a_star_rule_lights_it_even_under_gate(self) -> None:
        with patch.object(trifecta, "egress_confinement",
                          return_value={"mode": "gate", "egress": {"scope": "wide"}}):
            prof = self._prof()
        self.assertEqual(1, prof["bits"]["arbitrary_egress"])

    def test_an_unvetted_remote_still_lights_it(self) -> None:
        """La destinazione del remote NON è censita: non è perimetro, e il condotto
        è del canale, non dei verbi di qualcuno."""
        with patch.object(trifecta, "egress_confinement",
                          return_value={"mode": "gate", "egress": {"scope": "narrow"}}):
            prof = trifecta.context_profile(
                ["dev"], specs=[_ag("dev", EGRESSO)], tainted=False,
                remote_egress=True, channel_private_data=False)
        self.assertEqual(1, prof["bits"]["arbitrary_egress"])


if __name__ == "__main__":
    unittest.main()
