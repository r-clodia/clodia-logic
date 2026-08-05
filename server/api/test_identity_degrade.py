"""Un'identità PKI mancante non deve far morire la lista agenti.

Su un'istanza appena installata nessuno emette le identity key dei seed:
l'entrypoint dichiara "PKI bootstrap delegata al gateway", il gateway sa emetterle
e nessuno gliele chiede. Conseguenza osservata: `/api/agents` rispondeva
`500 Internal Server Error` — perché leggere i provider richiede un token, coniare
il token richiede un'identità, e il `PermissionError` risultante sfuggiva a
`_connected_safe`, che catturava solo `ProviderStoreError`.

La docstring di quella funzione prometteva già il degrado. L'eccezione sbagliata
la rendeva una promessa non mantenuta — che è peggio di nessuna promessa.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import agent_registry


class DegradeTests(unittest.TestCase):
    def test_a_missing_identity_degrades_instead_of_raising(self):
        with patch.object(agent_registry, "connected_provider_ids",
                          side_effect=PermissionError(
                              "agent 'clodia' senza identità (eseguire pki issue)")):
            self.assertEqual(agent_registry._connected_safe(), set())

    def test_an_unreachable_vault_still_degrades(self):
        from .agent_registry import ProviderStoreError
        with patch.object(agent_registry, "connected_provider_ids",
                          side_effect=ProviderStoreError("gateway giù")):
            self.assertEqual(agent_registry._connected_safe(), set())

    def test_an_unexpected_error_is_NOT_swallowed(self):
        """Non si degrada su tutto: un errore che non sappiamo interpretare deve
        risalire, altrimenti la pagina mente su uno stato che non ha verificato."""
        with patch.object(agent_registry, "connected_provider_ids",
                          side_effect=ValueError("qualcosa di nuovo")):
            with self.assertRaises(ValueError):
                agent_registry._connected_safe()

    def test_the_happy_path_is_unchanged(self):
        with patch.object(agent_registry, "connected_provider_ids",
                          return_value={"anthropic-api"}):
            self.assertEqual(agent_registry._connected_safe(), {"anthropic-api"})


if __name__ == "__main__":
    unittest.main()
