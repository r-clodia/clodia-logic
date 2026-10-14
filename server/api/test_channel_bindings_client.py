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


class _DataDir(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cb-client-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        p = patch.object(cb, "_data_dir", lambda: self.dir)
        p.start()
        self.addCleanup(p.stop)
        g = patch.object(cb, "_last_good", {})
        g.start()
        self.addCleanup(g.stop)

    def _scrivi(self, nome, payload):
        (self.dir / nome).write_text(json.dumps(payload), encoding="utf-8")


class LettoreTests(_DataDir):

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


class TheLegacyFileIsOnlyForAMissingRegistryTests(_DataDir):
    """Review blocker on #514: the legacy `telegram-bindings.json` must be read
    ONLY when `channel-bindings.json` does not exist. After the migration the
    legacy file is a stale copy: reading it whenever the new one is torn,
    unreadable or empty brings back the chats the owner unbound."""

    def test_a_torn_registry_does_not_resurrect_the_legacy_file(self):
        (self.dir / cb.FILENAME).write_text('{"version": 1, "connec', encoding="utf-8")
        self._scrivi(cb.LEGACY_FILENAME, {"-100": _B})
        with self.assertLogs("agent-server.channel_bindings", "WARNING"):
            self.assertEqual(cb.load(), {})
        self.assertFalse(cb.any_binding())

    def test_an_empty_connectors_map_does_not_resurrect_the_legacy_file(self):
        self._scrivi(cb.FILENAME, {"version": 1, "connectors": {}})
        self._scrivi(cb.LEGACY_FILENAME, {"-100": _B})
        self.assertEqual(cb.load(), {})
        self.assertFalse(cb.any_binding())

    def test_a_registry_of_the_wrong_shape_does_not_resurrect_the_legacy_file(self):
        self._scrivi(cb.FILENAME, {"version": 1, "connectors": ["telegram"]})
        self._scrivi(cb.LEGACY_FILENAME, {"-100": _B})
        with self.assertLogs("agent-server.channel_bindings", "WARNING"):
            self.assertEqual(cb.load(), {})

    def test_a_torn_registry_keeps_the_last_good_bindings(self):
        """A torn read (the gateway mid-write) must not drop the bound chats
        either: the last bindings read correctly stay in force."""
        good = {"version": 1, "connectors": {"telegram": {"-100": _B}}}
        self._scrivi(cb.FILENAME, good)
        self.assertEqual(cb.load(), {"telegram": {"-100": _B}})
        (self.dir / cb.FILENAME).write_text("{ torn", encoding="utf-8")
        self._scrivi(cb.LEGACY_FILENAME, {"-200": _B})
        with self.assertLogs("agent-server.channel_bindings", "WARNING"):
            self.assertEqual(cb.load(), {"telegram": {"-100": _B}})

    def test_the_last_good_bindings_cannot_be_mutated_by_callers(self):
        self._scrivi(cb.FILENAME, {"version": 1, "connectors": {"telegram": {"-100": _B}}})
        cb.load()["telegram"].clear()
        (self.dir / cb.FILENAME).write_text("{ torn", encoding="utf-8")
        with self.assertLogs("agent-server.channel_bindings", "WARNING"):
            self.assertEqual(cb.load(), {"telegram": {"-100": _B}})


if __name__ == "__main__":
    unittest.main()
