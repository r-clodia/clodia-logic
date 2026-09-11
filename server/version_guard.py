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

Uso in CI (vedi `.github/workflows/version.yml`):

    git show origin/main:server/__init__.py > /tmp/base_init.py
    python3 -m server.version_guard --base /tmp/base_init.py
"""

from __future__ import annotations

import argparse
import os
import re
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        required=True,
        help="sorgente di `server/__init__.py` come sta su origin/main "
        "(prodotto da `git show origin/main:server/__init__.py`)",
    )
    parser.add_argument(
        "--head", default=VERSION_FILE, help="il file di versione del branch"
    )
    args = parser.parse_args(argv)

    # Fail-closed: se il confronto non si può fare (file assente, versione
    # illeggibile o fuori formato) l'esito è rosso. Un guard che non sa
    # rispondere e dichiara verde è peggio di nessun guard: insegna a fidarsi.
    try:
        base = _versione_del_file(args.base)
        head = _versione_del_file(args.head)
    except (OSError, ValueError) as errore:
        print(f"::error::impossibile confrontare le versioni: {errore}")
        return 1

    try:
        piu_alta = is_newer(head, base)
    except ValueError as errore:
        print(f"::error::{errore}")
        return 1

    if not piu_alta:
        print(
            f"::error::__version__ del branch è {head}, su origin/main è già "
            f"{base}: il bump è stantio. Rileggi la versione su main e "
            f"ri-bumpa da lì — al merge git non segnalerebbe conflitto e la "
            f"release uscirebbe con un numero già usato."
        )
        return 1

    print(f"__version__ {head} > {base} su origin/main: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
