from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from . import topic_playbooks


class TopicPlaybookCompositionTests(unittest.TestCase):

    def test_suggest_team_grant_enables_intro_and_one_shot_marker(self) -> None:
        spec = SimpleNamespace(name="segretario")
        profile = SimpleNamespace(topics_defaults={}, vocabulary={})
        from ..agents import authz_grants
        with (
            patch.object(topic_playbooks.registry, "get_by_name", return_value=spec),
            patch.object(authz_grants, "has", return_value=True),
            patch.object(topic_playbooks.instance_profile, "load", return_value=profile),
            patch.object(topic_playbooks, "pills_for", return_value=[]),
        ):
            message = topic_playbooks.welcome_message(
                "nuovo", "Nuovo", "progetto", ["owner", "segretario"],
                contact_agent="segretario",
            )

        self.assertIn("squadra di agenti", message)
        self.assertIn("<!-- team-bootstrap=segretario -->", message)

    def test_agent_without_suggest_team_does_not_claim_composition(self) -> None:
        spec = SimpleNamespace(name="worker")
        from ..agents import authz_grants
        with patch.object(topic_playbooks.registry, "get_by_name", return_value=spec), \
                patch.object(authz_grants, "has", return_value=False):
            self.assertFalse(topic_playbooks._coordinator_can_compose("worker"))

    def test_a_mute_gateway_is_no_grant(self) -> None:
        """#656: gateway silent ⇒ no grant. There is no seed declaration to
        fall back on, so the welcome does not promise a team composition."""
        spec = SimpleNamespace(name="segretario")
        from ..agents import authz_grants
        with patch.object(topic_playbooks.registry, "get_by_name", return_value=spec), \
                patch.object(authz_grants, "has", return_value=None):
            self.assertFalse(topic_playbooks._coordinator_can_compose("segretario"))


if __name__ == "__main__":
    unittest.main()
