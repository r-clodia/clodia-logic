"""Il lettore dei binding di canale, e la finestra di deploy.

Gateway e agent-server si aggiornano separatamente: per qualche minuto quello
nuovo convive con quello vecchio. Senza fallback sul file storico, in quella
finestra ogni chat legata smette di essere servita — e smette in silenzio,
perché un messaggio che non arriva non lascia traccia da nessuna parte.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import channel_bindings_client as cb

_B = {"instance": "messaggero-1", "tier": "SEAL-1", "topic": "acme"}


class LettoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cb-client-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        p = patch.object(cb, "_data_dir", lambda: self.dir)
        p.start()
        self.addCleanup(p.stop)

    def _scrivi(self, nome, payload):
        (self.dir / nome).write_text(json.dumps(payload), encoding="utf-8")

    def test_legge_il_registro_per_connettore(self):
        self._scrivi(cb.FILENAME, {"version": 1, "connectors": {"telegram": {"-100": _B}}})
        self.assertEqual(cb.load(), {"telegram": {"-100": _B}})
        self.assertEqual(cb.for_connector("telegram"), {"-100": _B})
        self.assertEqual(cb.for_connector("signal"), {})
        self.assertTrue(cb.any_binding())

    def test_senza_registro_legge_ancora_il_file_storico(self):
        """La finestra di deploy: agent-server nuovo, gateway ancora vecchio."""
        self._scrivi(cb.LEGACY_FILENAME, {"-100": _B})
        self.assertEqual(cb.load(), {"telegram": {"-100": _B}})

    def test_con_il_registro_il_file_storico_non_si_guarda_piu(self):
        """Dopo la migrazione il file vecchio è solo una copia di cortesia: se
        lo si rileggesse, un binding staccato tornerebbe vivo."""
        self._scrivi(cb.FILENAME, {"version": 1, "connectors": {"telegram": {}}})
        self._scrivi(cb.LEGACY_FILENAME, {"-100": _B})
        self.assertEqual(cb.load(), {"telegram": {}})
        self.assertFalse(cb.any_binding())

    def test_niente_file_niente_binding(self):
        self.assertEqual(cb.load(), {})
        self.assertFalse(cb.any_binding())

    def test_un_file_illeggibile_non_ferma_il_relay(self):
        (self.dir / cb.FILENAME).write_text("{ rotto", encoding="utf-8")
        self.assertEqual(cb.load(), {})


if __name__ == "__main__":
    unittest.main()
