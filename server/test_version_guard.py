"""Il controllo che rende rosso un bump di versione stantio.

Il difetto che questi test descrivono è silenzioso: due PR indipendenti
alzano `server/__init__.py` allo STESSO numero, git non segnala conflitto
(la riga finale è identica in entrambi i rami) e la seconda che entra
pubblica una release col numero di quella prima. È successo davvero sul
6.198.0 — `git log` lo mostra ancora, mergiato dopo il 6.203.0.
"""

import io
import os
import pathlib
import re
import shutil
import subprocess
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


class PrendeLaBaseDalBranchDiDestinazione(unittest.TestCase):
    """`--base-ref`: la versione di confronto la legge il guard, da `origin`.

    Il numero contro cui confrontare deve essere quello che il branch di
    destinazione ha **al momento della run**, non quello del commit da cui il
    branch è partito: è proprio in quell'intervallo che entra l'altra PR con
    lo stesso numero. Un `git show` su un checkout superficiale non basta —
    in CI `origin/main` non è nemmeno presente — quindi il fetch fa parte del
    controllo, e qui viene esercitato per davvero su due repository veri.
    """

    def _git(self, *args, cwd):
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
        )

    def _scrivi_versione(self, radice, versione):
        path = os.path.join(radice, "server", "__init__.py")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f'__version__ = "{versione}"\nPLATFORM_VERSION = "8.0"\n')
        return path

    def _clone_con_origin_a(self, versione_su_main):
        """Un `origin` con quella versione su `main`, e un clone su cui lavorare."""
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        origin = os.path.join(base, "origin")
        os.makedirs(origin)
        self._git("init", "--quiet", "--initial-branch=main", cwd=origin)
        self._scrivi_versione(origin, versione_su_main)
        self._git("add", "-A", cwd=origin)
        self._git("commit", "--quiet", "-m", "base", cwd=origin)
        clone = os.path.join(base, "clone")
        self._git("clone", "--quiet", origin, clone, cwd=base)
        return clone

    def _esegui(self, clone, ref="main"):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            codice = version_guard.main(
                [
                    "--base-ref",
                    ref,
                    "--repo",
                    clone,
                    "--head",
                    os.path.join(clone, "server", "__init__.py"),
                ]
            )
        return codice, out.getvalue()

    def test_stesso_numero_del_branch_di_destinazione_esce_rosso(self):
        """Il caso che git non vede: due PR sullo stesso numero."""
        clone = self._clone_con_origin_a("6.204.0")
        self._scrivi_versione(clone, "6.204.0")
        codice, testo = self._esegui(clone)
        self.assertEqual(codice, 1)
        self.assertIn("6.204.0", testo)

    def test_numero_piu_alto_esce_verde(self):
        clone = self._clone_con_origin_a("6.204.0")
        self._scrivi_versione(clone, "6.205.0")
        codice, _ = self._esegui(clone)
        self.assertEqual(codice, 0)

    def test_vede_il_branch_com_e_ADESSO_non_da_dove_e_partito_il_branch(self):
        """Il cuore del difetto: il clone è fermo a 6.204.0, ma nel frattempo
        su `main` è entrata la PR che ha preso 6.205.0. Un confronto contro il
        punto di partenza direbbe verde; il fetch dice rosso."""
        clone = self._clone_con_origin_a("6.204.0")
        self._scrivi_versione(clone, "6.205.0")
        origin = os.path.join(os.path.dirname(clone), "origin")
        self._scrivi_versione(origin, "6.205.0")
        self._git("commit", "--quiet", "-am", "l'altra PR entra per prima", cwd=origin)
        codice, testo = self._esegui(clone)
        self.assertEqual(codice, 1)
        self.assertIn("6.205.0", testo)

    def test_ref_inesistente_esce_rosso_e_non_verde(self):
        """Fail-closed anche qui: se il fetch non riesce, il confronto non si
        può fare, e un guard che in quel caso dichiara verde insegna a fidarsi
        di un controllo che non ha controllato niente."""
        clone = self._clone_con_origin_a("6.204.0")
        self._scrivi_versione(clone, "6.205.0")
        codice, testo = self._esegui(clone, ref="ramo-che-non-esiste")
        self.assertEqual(codice, 1)
        self.assertIn("::error::", testo)


class IlGuardEAgganciatoAllaCI(unittest.TestCase):
    """Il guard è inerte finché qualcosa lo esegue: questo è quel qualcosa.

    Prima di clodia-platform#415 il modulo esisteva, i suoi test erano verdi e
    **nessuna macchina lo invocava** — il docstring rimandava a un
    `.github/workflows/version.yml` che non è mai esistito. L'aggancio vive nel
    `Makefile` e non in un workflow perché la credenziale con cui gli agenti
    pubblicano non ha lo scope `workflow` di GitHub e il remoto rifiuta ogni
    push che tocchi `.github/workflows/` (decisione dell'owner, 23 ago 2026):
    `make test` è il comando che la CI esegue davvero, ed è l'unico punto
    agganciabile da questo lato.

    Questi controlli sono statici di proposito: `make` non è installato
    ovunque giri la suite, e ciò che deve restare vero è il cablaggio.
    """

    MAKEFILE = pathlib.Path(__file__).resolve().parent.parent / "Makefile"

    def _testo(self):
        return self.MAKEFILE.read_text(encoding="utf-8")

    def _ricetta(self, target):
        """Le righe di ricetta di un target (quelle che iniziano con TAB)."""
        testo = self._testo()
        trovato = re.search(rf"^{target}:[^\n]*\n((?:\t[^\n]*\n?)*)", testo, re.MULTILINE)
        self.assertIsNotNone(trovato, f"il Makefile non definisce il target `{target}`")
        return trovato.group(1)

    def test_make_test_dipende_dal_controllo_di_versione(self):
        """Senza questa dipendenza il guard torna a non girare mai: la CI
        invoca `make test` e nient'altro."""
        trovato = re.search(r"^test:([^\n]*)", self._testo(), re.MULTILINE)
        self.assertIsNotNone(trovato, "il Makefile non definisce il target `test`")
        self.assertIn("version-check", trovato.group(1).split())

    def test_il_controllo_invoca_il_guard(self):
        self.assertIn("server.version_guard", self._ricetta("version-check"))

    def test_confronta_il_branch_di_destinazione_al_momento_della_run(self):
        """`--base-ref` e non un file preso dal punto di partenza del branch:
        è nell'intervallo fra i due che entra la PR che collide."""
        self.assertIn("--base-ref", self._ricetta("version-check"))

    def test_fuori_da_una_pull_request_non_confronta_niente(self):
        """`GITHUB_BASE_REF` è valorizzato solo nelle run di `pull_request`.
        Senza questa condizione, `make test` in locale pretenderebbe la rete e
        i push su `main` confronterebbero `main` con sé stesso, cioè rosso
        sempre."""
        self.assertIn("GITHUB_BASE_REF", self._ricetta("version-check"))


class IlRepositoryPassaIlProprioControllo(unittest.TestCase):
    """Il controllo vale solo se è agganciato al file vero: un guard che punta
    a un path sbagliato resta verde per sempre senza che nessuno se ne accorga."""

    def test_il_file_di_versione_di_default_esiste_ed_e_leggibile(self):
        with open(version_guard.VERSION_FILE, encoding="utf-8") as f:
            versione = version_guard.read_version(f.read())
        self.assertTrue(version_guard.is_newer(versione, "0.0.0"))


if __name__ == "__main__":
    unittest.main()
