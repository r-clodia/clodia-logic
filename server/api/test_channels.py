"""Test selezione risponditore del canale (rango + tag + clearance)."""
from __future__ import annotations

import unittest

from ..agents.models import AgentSpec
from . import channels


def _a(name, type="normal", clearance="P0", created_at=None, role=None) -> AgentSpec:
    return AgentSpec.model_validate({
        "name": name, "description": "d", "display_name": name, "type": type,
        "clearance": clearance, "created_at": created_at, "role": role,
        **({"model": "m", "system_prompt": "s.md"} if type != "human" else {}),
    })


class ResponderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agents = {
            "clodia": _a("clodia", "super", "P3", "2026-01-01T00:00:00Z"),
            "ophelia": _a("ophelia", "super", "P3", "2026-01-01T00:00:01Z"),
            "worker": _a("worker", "normal", "P1", "2026-02-01T00:00:00Z"),
            "owner": _a("owner", "human", role="superadmin"),
        }
        self._orig = channels.registry.get_by_name
        channels.registry.get_by_name = lambda n: self.agents.get(n)

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig

    def test_highest_rank_ai_responds(self) -> None:
        r = channels._pick_responder(["owner", "worker", "clodia"], "P0", None)
        self.assertEqual(r.name, "clodia")  # super > normal; umano non risponde

    def test_seniority_clodia_over_ophelia(self) -> None:
        r = channels._pick_responder(["ophelia", "clodia"], "P0", None)
        self.assertEqual(r.name, "clodia")

    def test_tag_overrides_rank(self) -> None:
        r = channels._pick_responder(["clodia", "worker"], "P0", "worker")
        self.assertEqual(r.name, "worker")

    def test_clearance_excludes_low(self) -> None:
        # canale P2: worker (P1) escluso, clodia (P3) ok
        r = channels._pick_responder(["worker", "clodia"], "P2", None)
        self.assertEqual(r.name, "clodia")
        # canale P2 con solo worker (P1) → nessun risponditore
        self.assertIsNone(channels._pick_responder(["worker"], "P2", None))

    def test_tag_low_clearance_falls_back(self) -> None:
        # worker taggato ma clearance insufficiente (P2) → escluso → fallback clodia
        r = channels._pick_responder(["worker", "clodia"], "P2", "worker")
        self.assertEqual(r.name, "clodia")

    def test_tag_parse(self) -> None:
        self.assertEqual(channels._tagged("ehi @worker puoi farlo?"), "worker")
        self.assertIsNone(channels._tagged("nessun tag qui"))

    def test_channel_meta_defaults_to_clodia(self) -> None:
        meta = channels._channel_meta({"title": "Aiuto"}, "owner", "support")
        self.assertEqual(meta["contact_agent"], "clodia")
        self.assertEqual(meta["participants"], ["owner", "clodia"])

    def test_channel_meta_uses_requested_contact_agent(self) -> None:
        meta = channels._channel_meta(
            {"title": "Aiuto", "type": "infra", "contact_agent": "Helpdesk"},
            "owner",
            "support",
        )
        self.assertEqual(meta["contact_agent"], "helpdesk")
        self.assertEqual(meta["participants"], ["owner", "helpdesk"])
        self.assertEqual(meta["type"], "infra")


if __name__ == "__main__":
    unittest.main()
