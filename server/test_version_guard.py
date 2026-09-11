"""Il controllo che rende rosso un bump di versione stantio.

Il difetto che questi test descrivono è silenzioso: due PR indipendenti
alzano `server/__init__.py` allo STESSO numero, git non segnala conflitto
(la riga finale è identica in entrambi i rami) e la seconda che entra
pubblica una release col numero di quella prima. È successo davvero sul
6.198.0 — `git log` lo mostra ancora, mergiato dopo il 6.203.0.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from . import version_guard


class LeggeIlNumeroDalSorgente(unittest.TestCase):
    """La versione si legge dal TESTO del file: quello di `origin/main` arriva
    da `git show`, senza che quel commit sia mai in working tree."""

    def test_legge_la_versione_dal_sorgente(self):
        sorgente = '__version__ = "6.204.0"\n\nPLATFORM_VERSION = "8.0"\n'
        self.assertEqual(version_guard.read_version(sorgente), "6.204.0")

    def test_non_confonde_platform_version_con_la_versione_del_componente(self):
        """`PLATFORM_VERSION` sta nello stesso file ed è un altro numero: se il
        controllo leggesse quello, confronterebbe due costanti che non si
        muovono a ogni PR e sarebbe verde per sempre."""
        sorgente = 'PLATFORM_VERSION = "8.0"\n__version__ = "6.204.0"\n'
        self.assertEqual(version_guard.read_version(sorgente), "6.204.0")

    def test_sorgente_senza_versione_e_un_errore_non_un_default(self):
        with self.assertRaises(ValueError):
            version_guard.read_version("PLATFORM_VERSION = \"8.0\"\n")


class ConfrontaLeVersioni(unittest.TestCase):
    def test_stesso_numero_non_passa(self):
        """Il caso di #405, quello che git non vede."""
        self.assertFalse(version_guard.is_newer("6.204.0", "6.204.0"))

    def test_numero_piu_basso_non_passa(self):
        self.assertFalse(version_guard.is_newer("6.203.0", "6.204.0"))

    def test_numero_piu_alto_passa(self):
        self.assertTrue(version_guard.is_newer("6.205.0", "6.204.0"))
        self.assertTrue(version_guard.is_newer("6.204.1", "6.204.0"))
        self.assertTrue(version_guard.is_newer("7.0.0", "6.204.0"))

    def test_confronto_numerico_non_lessicografico(self):
        """Con le stringhe "6.99.0" > "6.100.0": esattamente la fascia di
        numeri in cui vive questo repo (6.2xx)."""
        self.assertTrue(version_guard.is_newer("6.100.0", "6.99.0"))
        self.assertFalse(version_guard.is_newer("6.99.0", "6.100.0"))

    def test_componenti_mancanti_valgono_zero(self):
        self.assertFalse(version_guard.is_newer("6.204", "6.204.0"))
        self.assertTrue(version_guard.is_newer("6.205", "6.204.0"))

    def test_versione_malformata_e_un_errore(self):
        with self.assertRaises(ValueError):
            version_guard.is_newer("6.204.0-rc1", "6.204.0")


class LaRigaDiComando(unittest.TestCase):
    """Quello che la CI esegue davvero: due file, un exit code."""

    def _file(self, versione):
        fd, path = tempfile.mkstemp(suffix=".py")
        with os.fdopen(fd, "w") as f:
            f.write(f'__version__ = "{versione}"\nPLATFORM_VERSION = "8.0"\n')
        self.addCleanup(os.unlink, path)
        return path

    def _esegui(self, head, base):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            codice = version_guard.main(
                ["--base", self._file(base), "--head", self._file(head)]
            )
        return codice, out.getvalue()

    def test_bump_stantio_esce_rosso_e_dice_i_due_numeri(self):
        codice, testo = self._esegui(head="6.204.0", base="6.204.0")
        self.assertEqual(codice, 1)
        self.assertIn("6.204.0", testo)

    def test_bump_regolare_esce_verde(self):
        codice, _ = self._esegui(head="6.205.0", base="6.204.0")
        self.assertEqual(codice, 0)

    def test_base_illeggibile_esce_rosso(self):
        """Fail-closed: se il confronto non si può fare, non si dichiara verde."""
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            codice = version_guard.main(
                ["--base", "/non/esiste/__init__.py", "--head", self._file("6.205.0")]
            )
        self.assertEqual(codice, 1)


class IlRepositoryPassaIlProprioControllo(unittest.TestCase):
    """Il controllo vale solo se è agganciato al file vero: un guard che punta
    a un path sbagliato resta verde per sempre senza che nessuno se ne accorga."""

    def test_il_file_di_versione_di_default_esiste_ed_e_leggibile(self):
        with open(version_guard.VERSION_FILE, encoding="utf-8") as f:
            versione = version_guard.read_version(f.read())
        self.assertTrue(version_guard.is_newer(versione, "0.0.0"))


if __name__ == "__main__":
    unittest.main()
