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

    def _pick(self, storia: list[dict], trace: dict):
        with (
            patch.object(channels, "_provider_seal_ok", return_value=True),
            patch.object(channels, "_routing_mode", return_value="relevance"),
            patch.object(channels.responder_routing, "pick_by_exemplar",
                         return_value=None),
            patch.object(channels.responder_routing, "score_specialists",
                         return_value=self.scored),
            patch.object(channels.responder_routing, "decide", return_value=None),
            patch.object(channels.router_config, "load",
                         return_value=channels.router_config.RouterConfig(3, 0.80, 0.015)),
        ):
            return channels._pick_responder(
                ["clodia", "worker", "accountant"], "P0", None,
                "e per la fattura di luglio?", trace=trace,
                routing_messages=storia,
            )

    def test_the_last_responder_among_peers_takes_the_follow_up(self) -> None:
        trace: dict = {}
        picked = self._pick([
            _msg("davide", "human", "come sta andando?"),
            _msg("accountant-7", "ai", "il conto di giugno è chiuso"),
            _msg("davide", "human", "e per la fattura di luglio?"),
        ], trace)

        self.assertIsNotNone(picked)
        self.assertEqual(picked.name, "accountant")
        self.assertEqual(trace["mode"], "follow-up")
        self.assertNotIn("choices", trace)

    def test_a_stranger_to_the_tie_does_not_decide_it(self) -> None:
        # Ha risposto clodia, che NON è fra i due a pari merito: la continuità
        # non dice niente sul tie e la domanda resta legittima.
        trace: dict = {}
        picked = self._pick([
            _msg("clodia-3", "ai", "ci penso io"),
            _msg("davide", "human", "e per la fattura di luglio?"),
        ], trace)

        self.assertIsNone(picked)
        self.assertEqual(trace["mode"], "ambiguous")
        self.assertEqual(trace["choices"], ["worker", "accountant"])

    def test_the_router_dialog_is_not_a_conversation_to_continue(self) -> None:
        trace: dict = {}
        picked = self._pick([
            _msg("accountant-7", "ai", "il conto di giugno è chiuso"),
            _msg("router", "ai", "Routing ambiguo: chi deve rispondere?"),
            _msg("davide", "human", "e per la fattura di luglio?"),
        ], trace)

        self.assertIsNone(picked)
        self.assertEqual(trace["mode"], "ambiguous")

    def test_no_history_leaves_the_question_where_it_was(self) -> None:
        trace: dict = {}
        self.assertIsNone(self._pick([], trace))
        self.assertEqual(trace["mode"], "ambiguous")

    def test_the_plan_carries_the_window_down_to_the_picker(self) -> None:
        # Il piano è l'entry point vero del canale: se la finestra non arriva al
        # picker, la classificazione è codice morto.
        trace: dict = {}
        storia = [
            _msg("worker-2", "ai", "ho aperto il branch"),
            _msg("davide", "human", "e i test?"),
        ]
        with (
            patch.object(channels, "_provider_seal_ok", return_value=True),
            patch.object(channels, "_routing_mode", return_value="relevance"),
            patch.object(channels, "_multi_responder_enabled", return_value=False),
            patch.object(channels.responder_routing, "pick_by_exemplar",
                         return_value=None),
            patch.object(channels.responder_routing, "score_specialists",
                         return_value=self.scored),
            patch.object(channels.responder_routing, "decide", return_value=None),
            patch.object(channels.router_config, "load",
                         return_value=channels.router_config.RouterConfig(3, 0.80, 0.015)),
        ):
            plan = channels._routing_plan(
                ["clodia", "worker", "accountant"], "P0", "e i test?",
                trace=trace, routing_messages=storia,
            )

        self.assertEqual([spec.name for spec, _prompt in plan], ["worker"])
        self.assertEqual(trace["mode"], "follow-up")

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
