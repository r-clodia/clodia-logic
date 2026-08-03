"""Compatibility check must not compare a translated model id against the
catalogue's declared-model patterns.

Regression test for the production incident of 3 Aug 2026: `esperto-bandi` never
executed a turn in a SEAL-2 topic. Its model is `claude-opus-4-8`; the only
connected provider with SEAL >= 2 is `aws-region-eu`, which serves Claude through
Bedrock and therefore translates the id into the EU inference profile
`eu.anthropic.claude-opus-4-6-v1`. The check then asked whether the provider
supports *that*, against a catalogue pattern of `claude-*` — it does not match,
so the provider was rejected and the agent stayed silent.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import session


class DeclaredVsEffectiveModelTests(unittest.TestCase):
    KIND = "esperto-bandi"

    def _ensure(self, override):
        return session._ensure_runtime_provider(self.KIND, override)

    def test_bedrock_translated_id_does_not_reject_the_provider(self) -> None:
        # provider connected; catalogue only declares `claude-*`
        with patch.object(session, "_runtime_provider", return_value="aws-region-eu"), \
             patch.object(session, "_runtime_model",
                          return_value="eu.anthropic.claude-opus-4-6-v1"), \
             patch.object(session, "_declared_model", return_value="claude-opus-4-8"), \
             patch("server.api.providers.connected_provider_ids",
                   return_value={"aws-region-eu"}), \
             patch("server.api.providers.provider_supports_model",
                   side_effect=lambda p, m: bool(m and m.startswith("claude-"))):
            self._ensure({"provider": "aws-region-eu"})  # must not raise

    def test_genuinely_incompatible_model_still_raises(self) -> None:
        with patch.object(session, "_runtime_provider", return_value="aws-region-eu"), \
             patch.object(session, "_runtime_model", return_value="gpt-5.6-sol"), \
             patch.object(session, "_declared_model", return_value="gpt-5.6-sol"), \
             patch("server.api.providers.connected_provider_ids",
                   return_value={"aws-region-eu"}), \
             patch("server.api.providers.provider_supports_model",
                   side_effect=lambda p, m: bool(m and m.startswith("claude-"))):
            with self.assertRaises(RuntimeError):
                self._ensure({"provider": "aws-region-eu"})

    def test_provider_not_connected_still_raises(self) -> None:
        with patch.object(session, "_runtime_provider", return_value="aws-region-eu"), \
             patch.object(session, "_runtime_model", return_value="claude-opus-4-8"), \
             patch.object(session, "_declared_model", return_value="claude-opus-4-8"), \
             patch("server.api.providers.connected_provider_ids", return_value=set()):
            with self.assertRaises(session.ProviderNotConnected):
                self._ensure({"provider": "aws-region-eu"})


if __name__ == "__main__":
    unittest.main()
