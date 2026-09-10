from __future__ import annotations

import unittest

from .plugin_import import _sanitize_datastores


class SanitizeDatastoresTests(unittest.TestCase):
    def test_name_clearance_and_seeds_round_trip(self) -> None:
        out = _sanitize_datastores([
            {"path": "data/contacts.db", "purpose": "CRM contatti", "pii": True,
             "clearance": "seal-1", "seeds": ["messaggero", "clodia"]},
        ])
        self.assertEqual(out, [{
            "path": "data/contacts.db", "purpose": "CRM contatti", "pii": True,
            "backup": True, "clearance": "SEAL-1", "seeds": ["messaggero", "clodia"],
        }])

    def test_name_defaults_absent_when_not_declared(self) -> None:
        """Un pack che non dichiara `name` non forza nulla: il consumatore
        (clodia-tools) ricava lo stem dal path da solo."""
        out = _sanitize_datastores([{"path": "data/leads.db"}])
        self.assertNotIn("name", out[0])

    def test_explicit_name_is_kept(self) -> None:
        out = _sanitize_datastores([{"path": "data/contacts.db", "name": "contacts"}])
        self.assertEqual(out[0]["name"], "contacts")

    def test_unknown_clearance_tier_is_dropped_not_forced(self) -> None:
        """Un typo nel tier non deve allargare silenziosamente l'accesso: si
        scarta il campo (torna al default più restrittivo lato consumer),
        non si prova a interpretarlo."""
        out = _sanitize_datastores([{"path": "data/contacts.db", "clearance": "SEAL-9"}])
        self.assertNotIn("clearance", out[0])

    def test_seeds_must_be_a_list_of_strings(self) -> None:
        out = _sanitize_datastores([{"path": "data/contacts.db", "seeds": "messaggero"}])
        self.assertNotIn("seeds", out[0])

    def test_empty_seeds_list_is_dropped_not_kept_as_empty(self) -> None:
        out = _sanitize_datastores([{"path": "data/contacts.db", "seeds": []}])
        self.assertNotIn("seeds", out[0])

    def test_existing_fields_unaffected(self) -> None:
        """Nessuna regressione sui campi già presenti prima di questo cambio."""
        out = _sanitize_datastores([
            {"path": "data/leads.db", "purpose": "CRM lead", "pii": True, "backup": False},
        ])
        self.assertEqual(out, [{
            "path": "data/leads.db", "purpose": "CRM lead", "pii": True, "backup": False,
        }])


if __name__ == "__main__":
    unittest.main()
