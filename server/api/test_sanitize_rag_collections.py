"""Sanificazione di una collection RAG (`clodia-platform#343`): `tier`, and
`seeds` dropped (#669).

`tier`: normalizzato e validato come il `clearance` di un datastore
(`test_sanitize_datastores.py`). Il valore non resta una stringa qualsiasi:
viene scritto nel manifest registrato, letto dal provisioner
(`rag.create_collection`) e mostrato nella pagina Databases. Un `seal-1`
minuscolo o un `SEAL-9` inventato oggi passavano verbatim, e a valle non
combaciano con nessun gradino della scala.

`seeds`: no seed is mapped to a collection any more (clodia-platform#669). A
manifest that still declares a member list loads, and the list is dropped —
at import and in the API projection.
"""
from __future__ import annotations

import unittest

from .plugin_import import _sanitize_rag_collections
from .plugins import _plugin_item


def _tier(raw, **campi) -> str:
    return _sanitize_rag_collections([{"name": "prassi", **campi, "tier": raw}])[0]["tier"]


class SanitizeRagCollectionsTierTests(unittest.TestCase):

    def test_case_is_normalised_like_a_datastore_clearance(self) -> None:
        self.assertEqual("SEAL-1", _tier("seal-1"))
        self.assertEqual("SEAL-2", _tier("  Seal-2 "))

    def test_legacy_tier_is_translated(self) -> None:
        """`P0..P3` è la vecchia scala, tradotta ovunque in piattaforma
        (`channels._norm`, `agent_registry._norm_clearance`): un pack scritto
        allora non deve cadere nel ramo dei tier ignoti."""
        self.assertEqual("SEAL-1", _tier("P1"))

    def test_missing_tier_stays_the_documented_default(self) -> None:
        out = _sanitize_rag_collections([{"name": "prassi"}])
        self.assertEqual("SEAL-0", out[0]["tier"])

    def test_an_unknown_tier_closes_instead_of_opening(self) -> None:
        """Qui il default (`SEAL-0`) è il gradino PIÙ BASSO: scartare il campo
        come fa `_sanitize_datastores` allargherebbe l'accesso invece di
        chiuderlo. Un typo finisce quindi sul gradino più alto — visibile in UI
        e correggibile, mai un corpus aperto a tutti per una lettera."""
        self.assertEqual("SEAL-4", _tier("SEAL-9"))
        self.assertEqual("SEAL-4", _tier("pubblico"))
        self.assertEqual("SEAL-4", _tier(1))

    def test_the_rest_of_the_entry_is_untouched(self) -> None:
        """Nessuna regressione sui campi già sanificati prima di questo cambio."""
        out = _sanitize_rag_collections([{
            "name": " prassi-fiscale ", "description": "prassi",
            "resources": [{"url": "https://x/y.pdf", "doc_name": "y", "version": "1"}],
        }])
        self.assertEqual([{
            "name": "prassi-fiscale", "description": "prassi", "tier": "SEAL-0",
            "resources": [{"url": "https://x/y.pdf", "path": "", "doc_name": "y",
                           "version": "1", "type": "pdf", "meta": {}}],
        }], out)


class SanitizeRagCollectionsSeedsTests(unittest.TestCase):
    def test_a_declared_member_list_is_dropped(self) -> None:
        for seeds in (["aitiero", "archivista"], "aitiero", [], ["  "]):
            with self.subTest(seeds=seeds):
                out = _sanitize_rag_collections([
                    {"name": "prassi-fiscale", "tier": "SEAL-1", "seeds": seeds}])
                self.assertNotIn("seeds", out[0])

    def test_existing_fields_unaffected(self) -> None:
        """Nessuna regressione sui campi già presenti prima di questo cambio."""
        out = _sanitize_rag_collections([
            {"name": "eu-normativa", "description": "corpus UE", "tier": "SEAL-1",
             "resources": [{"url": "https://x/y.pdf", "doc_name": "y", "version": "1"}]},
        ])
        self.assertEqual(out, [{
            "name": "eu-normativa", "description": "corpus UE", "tier": "SEAL-1",
            "resources": [{
                "url": "https://x/y.pdf", "path": "", "doc_name": "y",
                "version": "1", "type": "pdf", "meta": {},
            }],
        }])


class PluginItemProjectionTests(unittest.TestCase):
    """The API projection carries no member list (#669)."""

    def _item(self, collection: dict) -> dict:
        item = _plugin_item(
            "demo-pack",
            {"manifest": {"rag_collections": [collection]}, "skills": [], "rules": []},
            {},
        )
        return item["rag_collections"][0]

    def test_seeds_are_never_projected(self) -> None:
        self.assertNotIn("seeds", self._item({"name": "prassi-fiscale", "seeds": ["aitiero"]}))
        self.assertNotIn("seeds", self._item({"name": "prassi-fiscale"}))


if __name__ == "__main__":
    unittest.main()
