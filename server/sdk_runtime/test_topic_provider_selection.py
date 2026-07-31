from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from . import session as S


class TopicProviderSelectionTests(unittest.TestCase):

    def test_topic_runtime_override_uses_min_cost_provider_for_tier(self):
        spec = SimpleNamespace(
            providers=["claude-team", "aws-region-eu"],
            provider=None,
            agent_sdk="claude",
            model="claude-sonnet-4-5",
            provider_models={},
        )
        with mock.patch.object(S, "_kind_spec", return_value=spec), \
             mock.patch("server.api.providers.connected_provider_ids",
                        return_value={"claude-team", "aws-region-eu"}):
            override = S.topic_runtime_override("clodia", "SEAL-1")

        self.assertEqual(override["provider"], "claude-team")
        self.assertEqual(override["topic_tier"], "SEAL-1")

    def test_topic_runtime_override_raises_when_no_provider_meets_tier(self):
        spec = SimpleNamespace(
            providers=["claude-team"],
            provider=None,
            agent_sdk="claude",
            model="claude-sonnet-4-5",
            provider_models={},
        )
        with mock.patch.object(S, "_kind_spec", return_value=spec), \
             mock.patch("server.api.providers.connected_provider_ids",
                        return_value={"claude-team"}):
            with self.assertRaises(S.ProviderNotConnected):
                S.topic_runtime_override("clodia", "SEAL-2")


if __name__ == "__main__":
    unittest.main()
