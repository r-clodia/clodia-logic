"""Test AgentSpec v2 (refactor logic-only, 12 giu 2026)."""
from __future__ import annotations

import unittest

from .models import AgentSpec


class AgentSpecV2Tests(unittest.TestCase):
    def _minimal(self, **extra) -> AgentSpec:
        payload = {
            "name": "demo",
            "description": "agente CAP minimale",
            "model": "claude-haiku-4-5",
            "display_name": "Demo",
            "capabilities": ["kanban-operations"],
            "system_prompt": "system-prompt.md",
        }
        payload.update(extra)
        return AgentSpec.model_validate(payload)

    def test_agent_sdk_defaults_to_claude(self):
        self.assertEqual(self._minimal().agent_sdk, "claude")

    def test_cap_fields_defaults(self):
        spec = self._minimal()
        self.assertEqual(spec.priority, 100)
        self.assertEqual(spec.cost_profile, "standard")
        self.assertEqual(spec.credentials, [])
        self.assertEqual(spec.volumes, [])

    def test_deprecated_fields_still_parse(self):
        # Retrocompatibilità: gli agent.yaml v3 restano validi (warning, non errore)
        spec = self._minimal(skills=["skills/x.md"], can_delegate_to=["other"])
        self.assertEqual(spec.skills, ["skills/x.md"])


if __name__ == "__main__":
    unittest.main()
