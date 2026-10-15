"""clodia-platform#546 — verbi del gateway pre-approvati dal seed.

Lo spawn Claude gira come root, il CLI non accetta `bypassPermissions` e applica
la modalità «auto»: il suo classificatore ha rifiutato a `tomato.sales-rep` le
scritture su Lead Tracker ([PII Data Handling], 15 ott 2026). Una regola
`allow` esplicita le risolve prima del classificatore; qui si fissa che la
regola nasce SOLO dal seed, SOLO per verbi del gateway, col nome esatto che il
modello vede.
"""
from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from server.agents.models import AgentSpec
from server.sdk_runtime import session as S

BASE = {"name": "tomato.sales-rep", "display_name": "Sales", "description": "test"}


class SeedFieldTests(unittest.TestCase):
    def _spec(self, tools):
        return AgentSpec(**BASE, auto_approved_tools=tools)

    def test_gateway_verbs_are_accepted_and_deduplicated(self):
        s = self._spec(["leads-mcp.update_lead", "leads-mcp.add_contact_log",
                        "leads-mcp.update_lead"])
        self.assertEqual(s.auto_approved_tools,
                         ["leads-mcp.update_lead", "leads-mcp.add_contact_log"])

    def test_absent_means_nothing_pre_approved(self):
        self.assertIsNone(AgentSpec(**BASE).auto_approved_tools)

    def test_native_tools_and_shapes_that_are_not_verbs_are_refused(self):
        for bad in ("Bash", "Write", "Bash(rm:*)", "mcp__clodia-tools__x_y",
                    "leads-mcp.*", "leads-mcp. update", "", ".update", "Leads.x"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self._spec([bad])


class ResolutionTests(unittest.TestCase):
    def test_exposed_name_replaces_dots(self):
        self.assertEqual(S.mcp_tool_name("leads-mcp.update_lead"),
                         "mcp__clodia-tools__leads-mcp_update_lead")
        self.assertEqual(S.mcp_tool_name("topic.read_file"),
                         "mcp__clodia-tools__topic_read_file")

    def test_seed_list_becomes_allow_rules(self):
        spec = SimpleNamespace(auto_approved_tools=["leads-mcp.update_lead"])
        with patch.object(S, "_kind_spec", return_value=spec):
            self.assertEqual(S._resolve_auto_approved("tomato.sales-rep"),
                             ["mcp__clodia-tools__leads-mcp_update_lead"])

    def test_no_field_or_unreadable_seed_pre_approves_nothing(self):
        with patch.object(S, "_kind_spec", return_value=SimpleNamespace()):
            self.assertEqual(S._resolve_auto_approved("x"), [])
        with patch.object(S, "_kind_spec", side_effect=RuntimeError("registry")):
            self.assertEqual(S._resolve_auto_approved("x"), [])


class WiringTests(unittest.TestCase):
    def test_the_claude_session_passes_them_as_allowed_tools(self):
        src = inspect.getsource(S.ChatSession.start)
        self.assertIn("_resolve_auto_approved(self.kind)", src)
        self.assertIn('opts_kwargs["allowed_tools"]', src)
        # Calcolate con la blocklist, prima del ramo root che decide il callback.
        self.assertLess(src.index("_resolve_auto_approved"),
                        src.index("_resolve_permission_mode(self.kind)"))


if __name__ == "__main__":
    unittest.main()
