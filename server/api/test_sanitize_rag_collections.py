"""`seeds:` su una collection RAG: member list dichiarata dal pack
(`clodia-platform#343`).

Stesso stampo di `test_sanitize_datastores.py`, perché è la stessa regola: la
lista di seed autorizzati di un datastore e quella di una collection si
sanificano allo stesso modo, e un campo ASSENTE non è un campo vuoto — è il
manifest che non si è pronunciato. La differenza sta a valle, nel gateway:
dichiarato = vincolante (restringe), non dichiarato = regime dei grant. Qui si
misura solo che il campo arrivi intero e che un `seeds` malformato non
sopravviva in una forma che il gate poi interpreterebbe.
"""
from __future__ import annotations

import unittest

from .plugin_import import _sanitize_rag_collections
from .plugins import _plugin_item


class SanitizeRagCollectionsSeedsTests(unittest.TestCase):
    def test_declared_seeds_round_trip(self) -> None:
        out = _sanitize_rag_collections([
            {"name": "prassi-fiscale", "tier": "SEAL-1", "seeds": ["aitiero", "archivista"]},
        ])
        self.assertEqual(out[0]["seeds"], ["aitiero", "archivista"])

    def test_seeds_absent_when_not_declared(self) -> None:
        """Il campo NON compare se il pack non lo dichiara: è la distinzione su
        cui si regge il gate a valle («il manifest non si è pronunciato» ≠
        «nessuno è autorizzato»)."""
        out = _sanitize_rag_collections([{"name": "prassi-fiscale"}])
        self.assertNotIn("seeds", out[0])

    def test_seeds_must_be_a_list_of_strings(self) -> None:
        out = _sanitize_rag_collections([
            {"name": "prassi-fiscale", "seeds": "aitiero"},
        ])
        self.assertNotIn("seeds", out[0])

    def test_empty_seeds_list_is_dropped_not_kept_as_empty(self) -> None:
        """Una lista vuota nel manifest è quasi sempre un residuo di editing, e
        tenuta come `[]` significherebbe «nessuno può entrare»: si scarta, come
        per i datastore."""
        out = _sanitize_rag_collections([{"name": "prassi-fiscale", "seeds": []}])
        self.assertNotIn("seeds", out[0])

    def test_blank_entries_are_dropped(self) -> None:
        out = _sanitize_rag_collections([
            {"name": "prassi-fiscale", "seeds": ["aitiero", "  ", "", "archivista"]},
        ])
        self.assertEqual(out[0]["seeds"], ["aitiero", "archivista"])

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
    """La proiezione API non deve buttare via `seeds` — è lo stesso difetto che
    `clodia-logic#412` ha corretto per i datastore: campo sanificato, persistito
    e poi perso nel punto che lo mostra."""

    def _item(self, collection: dict) -> dict:
        item = _plugin_item(
            "demo-pack",
            {"manifest": {"rag_collections": [collection]}, "skills": [], "rules": []},
            {},
        )
        return item["rag_collections"][0]

    def test_declared_seeds_are_projected(self) -> None:
        row = self._item({"name": "prassi-fiscale", "seeds": ["aitiero"]})
        self.assertEqual(row["seeds"], ["aitiero"])

    def test_undeclared_seeds_stay_absent_in_the_projection(self) -> None:
        row = self._item({"name": "prassi-fiscale"})
        self.assertNotIn("seeds", row)


if __name__ == "__main__":
    unittest.main()
