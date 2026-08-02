"""`GET /clodia/packs/{name}` — risoluzione del nome pack e fallback sul plugin.

Le dichiarazioni di setup sono per-PLUGIN, i due livelli non hanno gli stessi
nomi (`assetti-contabili` è un plugin del pack `studio-commercialista`): il
dettaglio deve risolvere anche partendo dal nome del plugin.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from . import packs

_PACKS = [
    {"name": "studio-commercialista",
     "plugins": [{"name": "assetti-contabili"}, {"name": "fatture"}]},
    {"name": "bandi-pack", "plugins": [{"name": "bandi-pack"}]},
]


class GetPackResolutionTest(unittest.TestCase):
    def _get(self, name: str):
        with patch.object(packs, "_list_packs", return_value=_PACKS):
            return asyncio.run(packs.get_pack(name))

    def test_pack_name_resolves_to_itself(self):
        res = self._get("studio-commercialista")
        self.assertEqual(res["name"], "studio-commercialista")
        self.assertNotIn("resolved_from_plugin", res)

    def test_plugin_name_resolves_to_container_pack(self):
        res = self._get("assetti-contabili")
        self.assertEqual(res["name"], "studio-commercialista")
        self.assertEqual(res["resolved_from_plugin"], "assetti-contabili")

    def test_pack_name_wins_over_omonymous_plugin(self):
        # `bandi-pack` è sia pack sia plugin: il pack ha la precedenza e non
        # viene marcato come risolto da plugin.
        res = self._get("bandi-pack")
        self.assertEqual(res["name"], "bandi-pack")
        self.assertNotIn("resolved_from_plugin", res)

    def test_unknown_name_is_404(self):
        res = self._get("ghost-pack")
        self.assertEqual(res.status_code, 404)

    def test_invalid_name_is_400(self):
        res = self._get("../etc")
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
