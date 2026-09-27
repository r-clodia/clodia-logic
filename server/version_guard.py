"""`__version__` deve essere più alto di quello che c'è già su `main`.

Perché serve un controllo eseguibile e non una convenzione. Due PR aperte
nello stesso giorno partono dallo stesso `main` e alzano entrambe
`server/__init__.py` allo stesso numero. Al merge git NON segnala conflitto:
la riga di partenza è identica e quella di arrivo pure, quindi non c'è niente
da risolvere. La seconda PR che entra pubblica una release col numero della
prima, e il difetto si scopre dopo, guardando `git log` — è successo sul
6.198.0, mergiato dopo il 6.203.0.

Il confronto va fatto contro `origin/main` AL MOMENTO DELLA RUN, non contro
il punto da cui il branch è partito: è esattamente nell'intervallo fra i due
che l'altra PR entra.

Uso in CI: lo invoca il target `version-check` del `Makefile`, che `make test`
ha come prerequisito — ed è `make test` ciò che `.github/workflows/tests.yml`
esegue a ogni pull request.

    python3 -m server.version_guard --base-ref "$GITHUB_BASE_REF"

L'aggancio sta nel `Makefile` e non in un workflow dedicato per una ragione
che non è di gusto: la credenziale con cui gli agenti pubblicano non ha lo
scope `workflow` di GitHub, e il remoto rifiuta qualunque push che tocchi
`.github/workflows/` (decisione dell'owner del 23 ago 2026 — dare quello scope
a un robot significa dargli di eseguire codice arbitrario dove si vedono i
secret del repository). Fino a clodia-platform#415 questo docstring rimandava
a un `.github/workflows/version.yml` che nessuno poteva scrivere: il guard è
rimasto scritto e inerte per settimane, e nel frattempo le collisioni sono
continuate.

Fuori da una run di `pull_request` il target non confronta niente: in locale
non c'è nulla contro cui confrontarsi e sui push a `main` il confronto sarebbe
`main` contro sé stesso, cioè rosso sempre.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

#: Il file che tiene la versione di QUESTO componente. Assoluto e derivato da
#: `__file__`: la CI lo invoca dalla radice del repo, i test da dove capita.
VERSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__init__.py")

#: `^__version__` ancorato a inizio riga: nello stesso file vive anche
#: `PLATFORM_VERSION`, che è il tag collettivo e non si muove a ogni PR.
_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)

# SHORTCUT: solo numeri e punti (`6.204.0`). Regge finché questo repo numera
#           così, che è come ha sempre numerato. Se un giorno servono le
#           pre-release (`6.205.0-rc1`), qui va messo un confronto semver
#           vero — oggi accettarle in silenzio significherebbe ordinarle a
#           caso, e un ordinamento sbagliato è peggio di un errore chiaro.
_NUMERO_RE = re.compile(r"^\d+(\.\d+)*$")


def read_version(sorgente: str) -> str:
    """La versione dichiarata nel TESTO di `server/__init__.py`.

    Sul testo e non sull'import, perché la versione di `origin/main` arriva da
    `git show` e quel commit non è in working tree.
    """
    trovato = _VERSION_RE.search(sorgente)
    if not trovato:
        raise ValueError("nessun `__version__` dichiarato nel sorgente")
    return trovato.group(1)


def parse_version(valore: str) -> tuple[int, ...]:
    if not _NUMERO_RE.match(valore.strip()):
        raise ValueError(f"versione non numerica: {valore!r}")
    return tuple(int(pezzo) for pezzo in valore.strip().split("."))


def is_newer(head: str, base: str) -> bool:
    """`head` è STRETTAMENTE maggiore di `base`?

    I componenti mancanti valgono zero, così `6.204` e `6.204.0` sono lo stesso
    numero invece che due.
    """
    a, b = parse_version(head), parse_version(base)
    lunghezza = max(len(a), len(b))
    riempi = lambda v: v + (0,) * (lunghezza - len(v))  # noqa: E731
    return riempi(a) > riempi(b)


def _versione_del_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return read_version(f.read())


#: Il path del file di versione DENTRO il repository, per leggerlo da un commit
#: che non è in working tree. Distinto da `VERSION_FILE`, che è il path su disco.
PATH_NEL_REPO = "server/__init__.py"


def sorgente_dal_ref(ref: str, repo: str = ".") -> str:
    """Il testo di `server/__init__.py` come sta ORA sul branch `ref`.

    Il fetch fa parte del controllo e non è un dettaglio dell'invocazione: in
    CI il checkout è superficiale e `origin/<base>` non è nemmeno presente, per
    cui un `git show origin/main:...` fallirebbe. `--depth=1` perché serve solo
    la punta: la storia non c'entra col confronto.
    """
    def _git(*args: str) -> str:
        fatto = subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )
        return fatto.stdout

    _git("fetch", "--quiet", "--depth=1", "origin", ref)
    return _git("show", f"FETCH_HEAD:{PATH_NEL_REPO}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    da_dove = parser.add_mutually_exclusive_group(required=True)
    da_dove.add_argument(
        "--base",
        help="sorgente di `server/__init__.py` come sta su origin/main "
        "(prodotto da `git show origin/main:server/__init__.py`)",
    )
    da_dove.add_argument(
        "--base-ref",
        help="il branch di destinazione (es. `$GITHUB_BASE_REF`): la versione "
        "la legge il guard, da `origin`, com'è in questo momento",
    )
    parser.add_argument(
        "--repo", default=".", help="la radice del repository su cui girare git"
    )
    parser.add_argument(
        "--head", default=VERSION_FILE, help="il file di versione del branch"
    )
    args = parser.parse_args(argv)

    # Fail-closed: se il confronto non si può fare (file assente, versione
    # illeggibile o fuori formato) l'esito è rosso. Un guard che non sa
    # rispondere e dichiara verde è peggio di nessun guard: insegna a fidarsi.
    try:
        if args.base_ref:
            base = read_version(sorgente_dal_ref(args.base_ref, args.repo))
        else:
            base = _versione_del_file(args.base)
        head = _versione_del_file(args.head)
    except subprocess.CalledProcessError as errore:
        print(
            f"::error::impossibile leggere {args.base_ref} da origin: "
            f"{(errore.stderr or '').strip() or errore}"
        )
        return 1
    except (OSError, ValueError) as errore:
        print(f"::error::impossibile confrontare le versioni: {errore}")
        return 1

    try:
        piu_alta = is_newer(head, base)
    except ValueError as errore:
        print(f"::error::{errore}")
        return 1

    dove = f"origin/{args.base_ref}" if args.base_ref else "origin/main"
    if not piu_alta:
        print(
            f"::error::__version__ del branch è {head}, su {dove} è già "
            f"{base}: il bump è stantio. Rileggi la versione su {dove} e "
            f"ri-bumpa da lì — al merge git non segnalerebbe conflitto e la "
            f"release uscirebbe con un numero già usato."
        )
        return 1

    print(f"__version__ {head} > {base} su {dove}: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
