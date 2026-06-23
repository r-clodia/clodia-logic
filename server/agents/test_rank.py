"""Test del rango agenti (chi parla di default in un canale)."""
from __future__ import annotations

import unittest

from .models import AgentSpec
from . import rank


def _a(name, type="normal", role=None, created_at=None) -> AgentSpec:
    return AgentSpec.model_validate({
        "name": name, "description": "d", "display_name": name,
        "type": type, "role": role, "created_at": created_at,
        **({"model": "m", "system_prompt": "s.md"} if type != "human" else {}),
    })


class RankTests(unittest.TestCase):
    def test_tier_order(self) -> None:
        self.assertGreater(rank.rank_tier(_a("d", "human", "superadmin")),
                           rank.rank_tier(_a("g", "human", "member")))
        self.assertGreater(rank.rank_tier(_a("g", "human", "member")),
                           rank.rank_tier(_a("clodia", "super")))
        self.assertGreater(rank.rank_tier(_a("clodia", "super")),
                           rank.rank_tier(_a("worker", "normal")))

    def test_seniority_tiebreak_clodia_over_ophelia(self) -> None:
        clodia = _a("clodia", "super", created_at="2026-01-01T00:00:00Z")
        ophelia = _a("ophelia", "super", created_at="2026-01-01T00:00:01Z")
        self.assertEqual(rank.highest([ophelia, clodia]).name, "clodia")

    def test_highest_picks_ai_super_over_normal(self) -> None:
        h = _a("owner", "human", "superadmin")
        clodia = _a("clodia", "super", created_at="2026-01-01T00:00:00Z")
        worker = _a("worker", "normal", created_at="2026-02-01T00:00:00Z")
        # tra i partecipanti AI, vince il super (gli umani non sono risponditori)
        self.assertEqual(rank.highest([h, worker, clodia]).name, "clodia")


if __name__ == "__main__":
    unittest.main()
