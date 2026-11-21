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


# ---------------------------------------------------------------------------
# clodia-platform#355, point 2: versions claimed by other open PRs. The GitHub
# API is never contacted: every test passes a fake `fetch(url, accept)`.
# ---------------------------------------------------------------------------

import json
import urllib.error
from unittest import mock

API = "https://api.example"
REPO = "r-clodia/repo"


class FakeGitHub:
    """Answers `/pulls` pages and `/contents/...?ref=<sha>` from a dict of
    `{number: version}`; `None` as a version means "file unreadable"."""

    def __init__(self, prs, page_size=100, broken_listing=False):
        self.prs = prs
        self.page_size = page_size
        self.broken_listing = broken_listing
        self.calls = []

    def __call__(self, url, accept):
        self.calls.append(url)
        if "/pulls?" in url:
            if self.broken_listing:
                raise version_guard.ApiError("HTTP 502 from " + url)
            self.base = re.search(r"base=([^&]+)", url).group(1)
            page = int(re.search(r"[?&]page=(\d+)", url).group(1))
            items = sorted(self.prs, reverse=True)
            chunk = items[(page - 1) * self.page_size : page * self.page_size]
            return json.dumps([{"number": n, "head": {"sha": f"sha{n}"}} for n in chunk])
        number = int(re.search(r"ref=sha(\d+)", url).group(1))
        version = self.prs[number]
        if version is None:
            raise version_guard.ApiError("HTTP 404 from " + url)
        return f'__version__ = "{version}"\nPLATFORM_VERSION = "8.0"\n'


def _quiet(fn, *args, **kwargs):
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        result = fn(*args, **kwargs)
    return result, out.getvalue()


class ReadsTheOpenPullRequests(unittest.TestCase):
    def test_lists_only_prs_towards_the_same_base(self):
        gh = FakeGitHub({11: "1.2.0"})
        version_guard.open_pull_requests(REPO, "main", gh, API)
        self.assertEqual(gh.base, "main")
        self.assertIn("state=open", gh.calls[0])
        self.assertIn("per_page=100", gh.calls[0])

    def test_follows_pagination_until_a_short_page(self):
        gh = FakeGitHub({n: "1.0.0" for n in range(1, 251)})
        prs = version_guard.open_pull_requests(REPO, "main", gh, API)
        self.assertEqual(len(prs), 250)
        self.assertEqual(sum("/pulls?" in c for c in gh.calls), 3)

    def test_reads_each_version_at_the_pr_head_and_skips_its_own(self):
        gh = FakeGitHub({10: "1.3.0", 11: "1.4.0", 12: "1.5.0"})
        claims, _ = _quiet(version_guard.claimed_versions, REPO, "main", 11, gh, API)
        self.assertEqual(claims, {10: "1.3.0", 12: "1.5.0"})
        self.assertFalse(any("ref=sha11" in c for c in gh.calls))
        self.assertTrue(any(c.endswith("contents/server/__init__.py?ref=sha10") for c in gh.calls))

    def test_an_unreadable_pr_is_a_warning_not_a_failure(self):
        """Somebody else's malformed branch must not block this PR."""
        gh = FakeGitHub({10: None, 12: "not-a-version", 13: "1.5.0"})
        claims, out = _quiet(version_guard.claimed_versions, REPO, "main", 11, gh, API)
        self.assertEqual(claims, {13: "1.5.0"})
        self.assertIn("::warning::", out)
        self.assertIn("#10", out)
        self.assertIn("#12", out)

    def test_a_listing_failure_raises(self):
        with self.assertRaises(version_guard.ApiError):
            version_guard.claimed_versions(REPO, "main", 1, FakeGitHub({}, broken_listing=True), API)


class DecidesWhoKeepsTheVersion(unittest.TestCase):
    def test_lower_pr_number_keeps_the_version(self):
        older, newer = version_guard.collisions(626, "6.367.1", {625: "6.367.1", 627: "6.367.1", 628: "6.368.0"})
        self.assertEqual((older, newer), ([625], [627]))

    def test_missing_components_count_as_zero(self):
        older, _ = version_guard.collisions(9, "1.2", {3: "1.2.0"})
        self.assertEqual(older, [3])

    def test_different_versions_do_not_collide(self):
        self.assertEqual(version_guard.collisions(9, "1.2.1", {3: "1.2.0"}), ([], []))

    def test_suggestion_bumps_the_same_component_above_every_claim(self):
        claimed = ["6.367.1", "6.368.0", "6.368.1"]
        self.assertEqual(version_guard.suggest_free("6.367.1", "6.367.0", claimed), "6.368.2")
        self.assertEqual(version_guard.suggest_free("6.368.0", "6.367.0", claimed), "6.369.0")
        self.assertEqual(version_guard.suggest_free("7.0.0", "6.367.0", claimed), "8.0.0")


class TheOpenPrCheck(unittest.TestCase):
    def _env(self, number=626, token="t"):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"pull_request": {"number": number}}, f)
        self.addCleanup(os.unlink, path)
        env = {"GITHUB_REPOSITORY": REPO, "GITHUB_EVENT_PATH": path, "GITHUB_API_URL": API}
        if token:
            env["GITHUB_TOKEN"] = token
        return env

    def test_collision_with_an_older_pr_fails_and_names_it(self):
        """The real case of 21 Nov 2026: logic#625 and #626 both on 6.367.1."""
        gh = FakeGitHub({625: "6.367.1", 624: "6.368.0", 627: "6.368.1"})
        code, out = _quiet(version_guard.check_open_prs, "6.367.1", "6.367.0", "main", self._env(626), gh)
        self.assertEqual(code, 1)
        self.assertIn("::error::", out)
        self.assertIn("#625", out)
        self.assertIn("6.368.2", out)

    def test_the_older_pr_stays_green_and_warns_about_the_newer(self):
        gh = FakeGitHub({626: "6.367.1"})
        code, out = _quiet(version_guard.check_open_prs, "6.367.1", "6.367.0", "main", self._env(625), gh)
        self.assertEqual(code, 0)
        self.assertIn("::warning::#626", out)

    def test_free_version_passes(self):
        gh = FakeGitHub({625: "6.367.1", 624: "6.368.0"})
        code, out = _quiet(version_guard.check_open_prs, "6.369.0", "6.367.0", "main", self._env(630), gh)
        self.assertEqual(code, 0)
        self.assertIn("ok", out)

    def test_api_failure_is_red_not_green(self):
        gh = FakeGitHub({}, broken_listing=True)
        code, out = _quiet(version_guard.check_open_prs, "6.369.0", "6.367.0", "main", self._env(630), gh)
        self.assertEqual(code, 1)
        self.assertIn("::error::", out)

    def test_without_a_token_it_says_how_to_wire_it(self):
        code, out = _quiet(version_guard.check_open_prs, "6.369.0", "6.367.0", "main", self._env(630, token=None))
        self.assertEqual(code, 1)
        self.assertIn("GITHUB_TOKEN", out)

    def test_outside_a_pull_request_it_fails(self):
        code, out = _quiet(version_guard.check_open_prs, "6.369.0", "6.367.0", "main", {"GITHUB_TOKEN": "t"})
        self.assertEqual(code, 1)

    def test_pr_number_falls_back_to_the_merge_ref(self):
        self.assertEqual(version_guard.pull_request_number(None, "refs/pull/42/merge"), 42)
        self.assertIsNone(version_guard.pull_request_number(None, "refs/heads/main"))

    def test_open_prs_requires_base_ref(self):
        with self.assertRaises(SystemExit):
            _quiet(version_guard.main, ["--base", "x", "--open-prs"])


class TheHttpLayer(unittest.TestCase):
    def _http_error(self, code, remaining):
        headers = {"x-ratelimit-remaining": remaining}
        return urllib.error.HTTPError("https://api.example/x", code, "no", headers, None)

    def test_rate_limit_is_reported_as_such(self):
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error(403, "0")):
            with self.assertRaisesRegex(version_guard.ApiError, "rate limit"):
                version_guard._fetch("https://api.example/x", "t", "application/json")

    def test_other_http_errors_carry_the_status(self):
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error(404, "999")):
            with self.assertRaisesRegex(version_guard.ApiError, "404"):
                version_guard._fetch("https://api.example/x", "t", "application/json")

    def test_sends_the_token_and_the_accept_header(self):
        seen = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout):
            seen["auth"] = request.get_header("Authorization")
            seen["accept"] = request.get_header("Accept")
            return Response(b"body")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(version_guard._fetch("https://api.example/x", "tok", "raw"), "body")
        self.assertEqual(seen, {"auth": "Bearer tok", "accept": "raw"})


class TheOpenPrCheckIsWiredIntoCi(unittest.TestCase):
    """Static on purpose, like `IlGuardEAgganciatoAllaCI`: what must stay true
    is the wiring, and an unwired check is code that no machine runs."""

    ROOT = pathlib.Path(__file__).resolve().parent.parent

    def test_version_check_asks_for_the_open_pr_check(self):
        recipe = re.search(r"^version-check:[^\n]*\n((?:\t[^\n]*\n?)*)",
                           (self.ROOT / "Makefile").read_text(encoding="utf-8"), re.MULTILINE)
        self.assertIsNotNone(recipe)
        self.assertIn("--open-prs", recipe.group(1))

    def test_the_workflow_grants_read_access_and_passes_the_token(self):
        workflow = (self.ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        self.assertRegex(workflow, r"(?m)^\s+pull-requests:\s*read\s*$")
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", workflow)
        self.assertNotRegex(workflow, r"(?m)^\s+pull-requests:\s*write")


if __name__ == "__main__":
    unittest.main()
