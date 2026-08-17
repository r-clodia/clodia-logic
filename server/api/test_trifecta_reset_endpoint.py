"""Il «reset trifecta» è una firma, non un silenziamento.

Richiesta dell'owner (17 ago 2026): «un bottoncino "reset trifecta" che riporta a
0/3 sotto la responsabilità dell'owner».
"""
from __future__ import annotations

import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ..agents import trifecta_reset as tr


class ResetStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._env = patch.dict(os.environ, {"CLODIA_DATA": self._tmp.name})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_a_reset_records_who_and_when(self) -> None:
        """Un azzeramento anonimo è indistinguibile da un difetto di calcolo."""
        v = tr.set_reset("SEAL-1", "ops", "davide", ["clodia", "dev"])
        self.assertEqual("davide", v["by"])
        self.assertTrue(v["at"])
        attivo = tr.active("SEAL-1", "ops", ["clodia", "dev"])
        self.assertEqual("davide", (attivo or {}).get("by"))

    def test_it_decays_when_a_participant_is_added(self) -> None:
        """Senza questo si azzererebbe un canale di tre agenti e poi ci si
        aggiungerebbe chi ha uscita arbitraria, tenendosi lo zero."""
        tr.set_reset("SEAL-1", "ops", "davide", ["clodia", "dev"])
        self.assertIsNone(tr.active("SEAL-1", "ops", ["clodia", "dev", "messaggero"]))

    def test_the_order_of_participants_does_not_matter(self) -> None:
        tr.set_reset("SEAL-1", "ops", "davide", ["clodia", "dev"])
        self.assertIsNotNone(tr.active("SEAL-1", "ops", ["dev", "clodia"]))

    def test_removing_a_participant_also_decays_it(self) -> None:
        """Anche togliere cambia la composizione: la firma valeva per QUELLA
        stanza, e una stanza diversa la deve richiedere."""
        tr.set_reset("SEAL-1", "ops", "davide", ["clodia", "dev"])
        self.assertIsNone(tr.active("SEAL-1", "ops", ["clodia"]))

    def test_clear_is_idempotent(self) -> None:
        tr.set_reset("SEAL-1", "ops", "davide", ["clodia"])
        self.assertTrue(tr.clear_reset("SEAL-1", "ops"))
        self.assertFalse(tr.clear_reset("SEAL-1", "ops"))
        self.assertIsNone(tr.active("SEAL-1", "ops", ["clodia"]))

    def test_another_channel_is_not_affected(self) -> None:
        tr.set_reset("SEAL-1", "ops", "davide", ["clodia"])
        self.assertIsNone(tr.active("SEAL-1", "altro", ["clodia"]))

    def test_an_unreadable_store_is_treated_as_absent(self) -> None:
        """Un file corrotto non deve azzerare un punteggio né rompere l'apertura
        di un canale: si considera «nessun reset», che è la direzione prudente."""
        p = tr._path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{non-json", encoding="utf-8")
        self.assertIsNone(tr.active("SEAL-1", "ops", ["clodia"]))


if __name__ == "__main__":
    unittest.main()
