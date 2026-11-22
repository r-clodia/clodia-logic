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
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

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


# ---------------------------------------------------------------------------
# Versions claimed by OTHER open pull requests (clodia-platform#355, point 2).
#
# Being above the target branch is not enough: two open PRs that both declare
# `X` are both above `main` and both green, until one merges and the other
# ships a release with a number already used. Nothing re-runs the second PR's
# check when `main` moves, so the collision has to be caught while both are
# still open: every PR reads the version each other open PR (towards the same
# base) declares at its head, and fails if its own is already taken.
#
# Precedence: the LOWER PR number keeps the version. If both PRs failed, both
# would re-bump and could collide again; with a deterministic owner exactly one
# of them has to move, and the message tells it which number is free.
#
# Cost: one paginated `GET /pulls?base=…` (100 PRs per page) plus one
# `GET /contents/<file>?ref=<head sha>` per other open PR, with the workflow's
# read-only GITHUB_TOKEN (1000 requests/hour per repository). No extra secret.
# Fork PRs work the same way: their head commit is readable through the base
# repository, and the token the workflow gets for a fork PR is read-only, which
# is all this needs.
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"


class ApiError(RuntimeError):
    """The GitHub API could not be read: the check cannot answer."""


def _fetch(url: str, token: str, accept: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "clodia-version-guard",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        remaining = error.headers.get("x-ratelimit-remaining") if error.headers else None
        if error.code in (403, 429) and remaining == "0":
            raise ApiError(
                f"GitHub API rate limit exhausted ({url}); re-run the job later"
            ) from error
        raise ApiError(f"HTTP {error.code} from {url}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise ApiError(f"cannot reach {url}: {error}") from error


def open_pull_requests(repo, base, fetch, api=GITHUB_API):
    """Open PRs of `repo` targeting `base`, as `(number, head_sha)`.

    `fetch(url, accept) -> str` is the only I/O, so tests pass a fake one.
    """
    found = []
    page = 1
    while True:
        query = urllib.parse.urlencode(
            {"state": "open", "base": base, "per_page": 100, "page": page}
        )
        body = fetch(f"{api}/repos/{repo}/pulls?{query}", "application/vnd.github+json")
        try:
            batch = json.loads(body)
            found.extend((int(pr["number"]), pr["head"]["sha"]) for pr in batch)
        except (ValueError, KeyError, TypeError) as error:
            raise ApiError(f"unexpected answer listing open PRs: {error}") from error
        if len(batch) < 100:
            return found
        page += 1


def claimed_versions(repo, base, own_number, fetch, api=GITHUB_API, path=PATH_NEL_REPO):
    """`{pr_number: version}` declared by every OTHER open PR towards `base`.

    Listing the PRs is fail-closed (raises `ApiError`). A single PR whose file
    cannot be read or parsed is skipped with a warning instead: that input comes
    from somebody else's branch, and a malformed `__version__` there must not
    make an unrelated PR unmergeable.
    """
    claims = {}
    for number, sha in open_pull_requests(repo, base, fetch, api):
        if number == own_number:
            continue
        url = f"{api}/repos/{repo}/contents/{path}?ref={sha}"
        try:
            version = read_version(fetch(url, "application/vnd.github.raw+json"))
            parse_version(version)
        except (ApiError, ValueError) as error:
            print(f"::warning::cannot read the version declared by #{number}: {error}")
            continue
        claims[number] = version
    return claims


def _same(a: str, b: str) -> bool:
    return not is_newer(a, b) and not is_newer(b, a)


def collisions(own_number, own_version, claims):
    """Split the PRs declaring `own_version` into `(older, newer)` numbers.

    `older` own the version (the current PR must re-bump); `newer` are the ones
    that will have to move.
    """
    same = sorted(n for n, v in claims.items() if _same(v, own_version))
    return [n for n in same if n < own_number], [n for n in same if n > own_number]


def suggest_free(head: str, base: str, claimed) -> str:
    """The next version above everything already claimed, bumping the same
    component this PR bumped against `base` (patch stays patch, minor stays
    minor). Above the maximum and not just "the first gap", so the suggestion
    does not turn stale as soon as the PRs below it merge."""
    h, b = parse_version(head), parse_version(base)
    width = max([len(h), len(b)] + [len(parse_version(v)) for v in claimed])
    pad = lambda v: v + (0,) * (width - len(v))  # noqa: E731
    h, b = pad(h), pad(b)
    position = next((i for i in range(width) if h[i] != b[i]), width - 1)
    top = max([h, b] + [pad(parse_version(v)) for v in claimed])
    bumped = top[:position] + (top[position] + 1,) + (0,) * (width - position - 1)
    return ".".join(str(n) for n in bumped)


def pull_request_number(event_path: str | None, ref: str | None) -> int | None:
    """The PR number from the event payload, or from `refs/pull/<n>/merge`."""
    if event_path:
        try:
            with open(event_path, encoding="utf-8") as f:
                number = json.load(f).get("pull_request", {}).get("number")
            if number:
                return int(number)
        except (OSError, ValueError):
            pass
    match = re.match(r"^refs/pull/(\d+)/", ref or "")
    return int(match.group(1)) if match else None


def check_open_prs(head, base_version, base_ref, env, fetch=None) -> int:
    """Exit code of the open-PR check. `env` is `os.environ` in CI."""
    repo = env.get("GITHUB_REPOSITORY")
    number = pull_request_number(env.get("GITHUB_EVENT_PATH"), env.get("GITHUB_REF"))
    token = env.get("GITHUB_TOKEN")
    if not (repo and number):
        print("::error::open-PR check: not running on a pull_request event "
              "(GITHUB_REPOSITORY / PR number missing)")
        return 1
    if fetch is None:
        if not token:
            print("::error::open-PR check: GITHUB_TOKEN is not set; the workflow "
                  "must pass `GITHUB_TOKEN: ${{ github.token }}` to this step")
            return 1
        fetch = lambda url, accept: _fetch(url, token, accept)  # noqa: E731
    api = env.get("GITHUB_API_URL") or GITHUB_API
    try:
        claims = claimed_versions(repo, base_ref, number, fetch, api)
    except ApiError as error:
        print(f"::error::open-PR check: {error}")
        return 1

    older, newer = collisions(number, head, claims)
    if older:
        owners = ", ".join(f"#{n}" for n in older)
        free = suggest_free(head, base_version, list(claims.values()))
        print(
            f"::error::__version__ {head} is already declared by open PR {owners} "
            f"towards {base_ref}. The lower PR number keeps it: re-bump this PR "
            f"(first free: {free})."
        )
        return 1
    for n in newer:
        print(f"::warning::#{n} also declares {head}; it is newer than #{number} "
              f"and its own check asks it to re-bump")
    if newer:
        print(f"__version__ {head}: #{number} is the oldest open PR declaring it "
              f"towards {base_ref}, so it keeps it: ok")
    else:
        print(f"__version__ {head} not claimed by the other {len(claims)} open "
              f"PR(s) towards {base_ref}: ok")
    return 0


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
    parser.add_argument(
        "--open-prs",
        action="store_true",
        help="also fail if another open PR towards --base-ref already declares "
        "this version (needs GITHUB_TOKEN, GITHUB_REPOSITORY and the "
        "pull_request event, i.e. a CI run)",
    )
    args = parser.parse_args(argv)
    if args.open_prs and not args.base_ref:
        parser.error("--open-prs needs --base-ref")

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
    if args.open_prs:
        return check_open_prs(head, base, args.base_ref, os.environ)
    return 0


if __name__ == "__main__":
    sys.exit(main())
