"""Il coordinatore è dichiarato: la regola non dipende dal rango né dall'ordine.

I casi sono quelli della ruling dell'11 ago 2026 (router-notebook R10). L'ultimo
è quello che l'issue #188 chiama per nome: la regola non deve *dedurre* nulla —
se il coordinatore non c'è, lo dice invece di scegliere il più forte.
"""
from __future__ import annotations

import unittest

from . import coordinator
from .models import AgentSpec


def _a(name, created_at=None) -> AgentSpec:
    return AgentSpec.model_validate({
        "name": name, "description": "d", "display_name": name,
        "type": "bot", "created_at": created_at,
        "model": "m", "system_prompt": "s.md",
    })


class CoordinatorTests(unittest.TestCase):
    def test_the_secretary_is_the_default(self) -> None:
        """#667: without a choice of the room the secretary leads, Clodia too
        being in the room."""
        spec, reason = coordinator.pick([_a("segretario"), _a("clodia")])
        self.assertEqual(spec.name, "segretario")
        self.assertIn("segretario", reason)

    def test_the_room_choice_wins(self) -> None:
        for chosen in ("clodia", "ophelia"):
            spec, reason = coordinator.pick(
                [_a("segretario"), _a("clodia"), _a("ophelia")], chosen)
            self.assertEqual(spec.name, chosen)
            self.assertIn("scelto", reason)

    def test_a_choice_that_cannot_serve_falls_back(self) -> None:
        spec, _reason = coordinator.pick([_a("clodia"), _a("worker")], "segretario")
        self.assertEqual(spec.name, "clodia")
        spec, _reason = coordinator.pick([_a("segretario")], "ophelia")
        self.assertEqual(spec.name, "segretario")

    def test_room_choice_reads_only_declared_seeds(self) -> None:
        self.assertEqual(coordinator.room_choice({"orchestrator": "Clodia"}), "clodia")
        self.assertIsNone(coordinator.room_choice({"orchestrator": "worker"}))
        self.assertIsNone(coordinator.room_choice({}))
        self.assertIsNone(coordinator.room_choice(None))

    def test_segretario_when_clodia_absent(self) -> None:
        spec, _reason = coordinator.pick([_a("worker"), _a("segretario")])
        self.assertEqual(spec.name, "segretario")

    def test_order_of_the_list_does_not_decide(self) -> None:
        """Non è «il primo partecipante»: è la precedenza dichiarata."""
        spec, _reason = coordinator.pick([_a("clodia"), _a("segretario")])
        self.assertEqual(spec.name, "segretario")

    def test_rank_does_not_decide(self) -> None:
        """Un bot più anziano di entrambi non diventa coordinatore per anzianità."""
        spec, _reason = coordinator.pick([
            _a("worker", created_at="2020-01-01T00:00:00Z"),
            _a("segretario", created_at="2026-01-01T00:00:00Z"),
        ])
        self.assertEqual(spec.name, "segretario")

    def test_none_declared_returns_none_with_a_reason(self) -> None:
        spec, reason = coordinator.pick([_a("worker"), _a("accountant")])
        self.assertIsNone(spec)
        self.assertTrue(reason)

    def test_empty_room(self) -> None:
        self.assertEqual(coordinator.pick([]), (None, coordinator.pick([])[1]))
        self.assertIsNone(coordinator.pick([])[0])
        self.assertIsNone(coordinator.pick(None)[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
