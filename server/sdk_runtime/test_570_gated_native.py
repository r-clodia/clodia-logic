"""clodia-platform#570 — a gated native tool (WebSearch) is used only after a
person's yes, asked through the gateway; any failure to ask is a no."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from . import session as S


def _run(gate, name, inp=None):
    return asyncio.run(gate(name, inp or {}, MagicMock()))


class GatedPermissionTests(unittest.TestCase):
    def test_a_gated_tool_asks_and_follows_the_answer(self) -> None:
        asked = []

        async def yes(tool, inp):
            asked.append((tool, inp))
            return True, ""

        async def no(tool, inp):
            return False, "negato dall'operatore"

        g = S._permission_gate(["Bash(rm:*)"], gated={"WebSearch"}, consent=yes)
        self.assertIsInstance(_run(g, "WebSearch", {"query": "q"}), PermissionResultAllow)
        self.assertEqual(asked, [("WebSearch", {"query": "q"})])
        g = S._permission_gate([], gated={"WebSearch"}, consent=no)
        r = _run(g, "WebSearch")
        self.assertIsInstance(r, PermissionResultDeny)
        self.assertIn("negato", r.message)

    def test_a_failure_to_ask_is_a_no(self) -> None:
        async def boom(tool, inp):
            raise RuntimeError("gateway down")

        g = S._permission_gate([], gated={"WebSearch"}, consent=boom)
        self.assertIsInstance(_run(g, "WebSearch"), PermissionResultDeny)

    def test_without_a_consent_function_the_tool_stays_denied(self) -> None:
        g = S._permission_gate(["WebSearch"], gated={"WebSearch"}, consent=None)
        self.assertIsInstance(_run(g, "WebSearch"), PermissionResultDeny)

    def test_other_denials_are_unchanged(self) -> None:
        async def yes(tool, inp):
            return True, ""
        g = S._permission_gate(["WebFetch"], gated={"WebSearch"}, consent=yes)
        self.assertIsInstance(_run(g, "WebFetch"), PermissionResultDeny)
        self.assertIsInstance(_run(g, "Read"), PermissionResultAllow)


class SeedTests(unittest.TestCase):
    def test_every_seed_reaches_websearch_through_the_room(self) -> None:
        """Option C (owner, 26 Oct 2026): the room's `web.search` decides, so
        every seed gets WebSearch behind the gateway's consent — declared or
        not — and nothing else is taken."""
        for decl in (["WebSearch", "Bash", "WebFetch"], None, []):
            spec = MagicMock(gated_native_tools=decl)
            with patch("server.agents.loader.registry.get_by_name", return_value=spec):
                self.assertEqual(S._resolve_native_gated("chiunque"), ["WebSearch"])

    def test_the_spec_accepts_the_field(self) -> None:
        from ..agents.models import AgentSpec
        f = AgentSpec.model_fields
        self.assertIn("gated_native_tools", f)

    def test_clodia_declares_websearch_gated_not_free(self) -> None:
        import yaml
        from ..config import workspace_path
        doc = yaml.safe_load(workspace_path(
            "catalogs/packs/base-pack/agents/clodia/agent.yaml").read_text(encoding="utf-8"))
        self.assertIn("WebSearch", doc.get("gated_native_tools") or [])
        self.assertNotIn("WebSearch", doc.get("native_tools") or [])
        self.assertIn("native.consent", doc.get("tool_permissions") or [])


if __name__ == "__main__":
    unittest.main()
