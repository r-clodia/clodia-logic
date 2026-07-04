"""Test del rango agenti (chi parla di default in un canale)."""
from __future__ import annotations

import unittest

from .models import AgentSpec
from . import rank
from .loader import AgentRegistry, registry


def _a(name, type="normal", role=None, created_at=None, telegram=None) -> AgentSpec:
    return AgentSpec.model_validate({
        "name": name, "description": "d", "display_name": name,
        "type": type, "role": role, "created_at": created_at,
        **({"telegram": telegram} if telegram is not None else {}),
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


class RankOfNameTests(unittest.TestCase):
    """`rank_of_name` risolve via registry ed è fail-closed sugli ignoti."""

    def setUp(self) -> None:
        self._added = []
        for spec in (_a("owner_h", "human", "superadmin"),
                     _a("member_h", "human", "member"),
                     _a("clodia_t", "super"),
                     _a("worker_t", "normal")):
            registry._agents[spec.name] = spec
            self._added.append(spec.name)

    def tearDown(self) -> None:
        for n in self._added:
            registry._agents.pop(n, None)

    def test_resolves_tiers(self) -> None:
        self.assertEqual(rank.rank_of_name("owner_h"), 4)
        self.assertEqual(rank.rank_of_name("member_h"), 3)
        self.assertEqual(rank.rank_of_name("clodia_t"), 2)
        self.assertEqual(rank.rank_of_name("worker_t"), 1)

    def test_outside_lattice_is_zero(self) -> None:
        # proxy, nome ignoto, None, vuoto → fuori dal lattice (deny)
        self.assertEqual(rank.rank_of_name("tg:123456"), rank.RANK_OUTSIDE)
        self.assertEqual(rank.rank_of_name("chi_non_esiste"), rank.RANK_OUTSIDE)
        self.assertEqual(rank.rank_of_name(None), rank.RANK_OUTSIDE)
        self.assertEqual(rank.rank_of_name(""), rank.RANK_OUTSIDE)


class GetByTelegramTests(unittest.TestCase):
    """Lookup inverso tg_user → principal HUMAN registrato (o None = proxy)."""

    def _reg(self) -> AgentRegistry:
        reg = AgentRegistry()
        for spec in (_a("davide", "human", "superadmin", telegram="@Davide_C"),
                     _a("mara", "human", "member", telegram="123456"),
                     _a("clodia", "super", telegram="@clodia_bot")):
            reg._agents[spec.name] = spec
        return reg

    def test_matches_human_by_handle_tolerant(self) -> None:
        reg = self._reg()
        self.assertEqual(reg.get_by_telegram("davide_c").name, "davide")   # no @, lower
        self.assertEqual(reg.get_by_telegram("@Davide_C").name, "davide")
        self.assertEqual(reg.get_by_telegram(" 123456 ").name, "mara")     # chat_id

    def test_proxy_and_ai_are_not_matched(self) -> None:
        reg = self._reg()
        self.assertIsNone(reg.get_by_telegram("sconosciuto"))   # non registrato = proxy
        self.assertIsNone(reg.get_by_telegram("clodia_bot"))    # è un AI, non un human
        self.assertIsNone(reg.get_by_telegram(None))
        self.assertIsNone(reg.get_by_telegram(""))


if __name__ == "__main__":
    unittest.main()
