"""Test AgentSpec v2 (refactor logic-only, 12 giu 2026)."""
from __future__ import annotations

import unittest

from pydantic import ValidationError

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

    def test_legacy_agent_types_normalize_to_bot(self):
        self.assertEqual(self._minimal(type="normal").type, "bot")
        self.assertEqual(self._minimal(type="super").type, "bot")
        self.assertEqual(self._minimal(type="bot").type, "bot")

        human = self._minimal(type="human", model=None)
        self.assertEqual(human.type, "human")

    def test_proxy_is_a_registered_non_runtime_principal(self):
        spec = AgentSpec.model_validate({
            "name": "webhook",
            "description": "Sistema terzo ammesso nel topic",
            "display_name": "Webhook",
            "type": "proxy",
            "tool_permissions": ["topic.post_message"],
        })
        self.assertEqual(spec.type, "proxy")
        self.assertIsNone(spec.model)
        self.assertIsNone(spec.system_prompt)
        self.assertIsNone(spec.memory)

    def test_proxy_rejects_runtime_and_extra_tools(self):
        with self.assertRaises(ValidationError):
            AgentSpec.model_validate({
                "name": "webhook",
                "description": "Sistema terzo",
                "display_name": "Webhook",
                "type": "proxy",
                "model": "claude-haiku-4-5",
            })
        with self.assertRaises(ValidationError):
            AgentSpec.model_validate({
                "name": "webhook",
                "description": "Sistema terzo",
                "display_name": "Webhook",
                "type": "proxy",
                "tool_permissions": ["topic.read_file"],
            })


if __name__ == "__main__":
    unittest.main()
