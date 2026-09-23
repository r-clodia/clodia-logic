"""Un follow-up non è un tie (clodia-platform#264).

Una persona replica alla risposta di un bot senza rifare la menzione. Il routing
per rilevanza trova due candidati entro il margine, dichiara ambiguità e chiede
in chat con le pills — ma la domanda ha già una risposta scritta nel canale: fra
i pari, uno ha appena parlato. Qui si verifica che quel caso venga classificato
da solo, e che le due guardie tengano: se l'ultimo che ha parlato non è fra i
pari, o se in mezzo si è messo un altro agente, la domanda resta.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from ..agents.models import AgentSpec
from . import channels


def _a(name: str, tipo: str = "normal", clearance: str = "P0") -> AgentSpec:
    return AgentSpec.model_validate({
        "name": name, "description": "d", "display_name": name, "type": tipo,
        "clearance": clearance, "model": "m", "system_prompt": "s.md",
    })


def _msg(author: str, kind: str, text: str) -> dict:
    return {"id": author + "-" + kind, "author": author, "kind": kind, "text": text}


class FollowUpBreaksTheTieTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agents = {
            "clodia": _a("clodia", "super", "P3"),
            "worker": _a("worker"),
            "accountant": _a("accountant"),
            "davide": _a("davide", "human"),
        }
        self.scored = [
            (self.agents["worker"], 0.91),
            (self.agents["accountant"], 0.905),
        ]
        self._orig = channels.registry.get_by_name
        channels.registry.get_by_name = lambda n: self.agents.get(n)
        self.addCleanup(lambda: setattr(channels.registry, "get_by_name", self._orig))
        self._known = channels._is_known_seed
        channels._is_known_seed = lambda n: n in self.agents
        self.addCleanup(lambda: setattr(channels, "_is_known_seed", self._known))

    # I cinque test end-to-end che vivevano qui («chi risponde in caso di tie
    # per rilevanza / follow-up / storia vuota») sono ritirati: modello nave
    # (clodia-platform#389), un messaggio non indirizzato non è più deciso
    # dalla rilevanza — cade sempre sul coordinatore dichiarato, mai su
    # un'ambiguità fra specialisti né su un follow-up fra pari. La funzione
    # `_follow_up_pick` resta viva e testata a sé in `FollowUpUnitTests` sotto
    # (segnale consultivo, non più nel percorso decisionale di
    # `_pick_responder`).

    def test_an_unaddressed_message_never_reopens_a_tie_between_specialists(self) -> None:
        """Regressione diretta del modello nave: stessi punteggi ravvicinati
        di prima (che aprivano un'ambiguità), stesso storico di follow-up —
        ora non c'è nessuna ambiguità e nessun follow-up: risponde sempre il
        coordinatore."""
        trace: dict = {}
        with (
            patch.object(channels, "_provider_seal_ok", return_value=True),
            patch.object(channels, "_routing_mode", return_value="relevance"),
            patch.object(channels.responder_routing, "score_specialists",
                         return_value=self.scored),
        ):
            picked = channels._pick_responder(
                ["clodia", "worker", "accountant"], "P0", None,
                "e per la fattura di luglio?", trace=trace,
                routing_messages=[
                    _msg("davide", "human", "come sta andando?"),
                    _msg("accountant-7", "ai", "il conto di giugno è chiuso"),
                    _msg("davide", "human", "e per la fattura di luglio?"),
                ],
            )

        self.assertEqual(picked.name, "clodia")
        self.assertEqual(trace["mode"], "coordinator")
        self.assertNotIn("choices", trace)

    def test_the_decision_is_counted_as_relevance_not_as_rank(self) -> None:
        visto: dict = {}

        def _record(origin, chosen, **kw):
            visto.update({"origin": origin, "chosen": chosen, **kw})

        with patch.object(channels.routing_feedback, "record_decision",
                          side_effect=_record):
            channels._track_routing_decision(
                {"tier": "P0", "name": "canale", "mode": "follow-up",
                 "chosen": "accountant"})

        self.assertEqual(visto.get("origin"), "relevance")
        self.assertEqual(visto.get("mode"), "follow-up")


class FollowUpUnitTests(unittest.TestCase):
    """`_follow_up_pick` da solo: la forma dei messaggi è quella del topic."""

    def setUp(self) -> None:
        self.worker = _a("worker")
        self.accountant = _a("accountant")
        self.candidates = [(self.worker, 0.91), (self.accountant, 0.905)]
        self._known = channels._is_known_seed
        channels._is_known_seed = lambda n: n in {"worker", "accountant", "clodia"}
        self.addCleanup(lambda: setattr(channels, "_is_known_seed", self._known))

    def test_the_spawn_label_resolves_to_its_seed(self) -> None:
        for autore in ("worker-9", "worker#9", "worker"):
            with self.subTest(autore=autore):
                got = channels._follow_up_pick(
                    [_msg(autore, "ai", "fatto"),
                     _msg("davide", "human", "e poi?")],
                    self.candidates,
                )
                self.assertIsNotNone(got)
                self.assertEqual(got[0].name, "worker")

    def test_a_single_candidate_is_not_a_tie(self) -> None:
        self.assertIsNone(channels._follow_up_pick(
            [_msg("worker-9", "ai", "fatto")], [(self.worker, 0.91)]))

    def test_empty_agent_messages_are_skipped_not_trusted(self) -> None:
        got = channels._follow_up_pick(
            [_msg("worker-9", "ai", "fatto"),
             _msg("accountant-1", "ai", "   "),
             _msg("davide", "human", "e poi?")],
            self.candidates,
        )
        self.assertIsNotNone(got)
        self.assertEqual(got[0].name, "worker")


if __name__ == "__main__":
    unittest.main()
