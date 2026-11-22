"""The platform's deny list for the file tools of a spawn (clodia-platform#665).

Measured on terra on 10 Nov 2026: the rules that were meant to keep spawns off
the secrets never protected anything. `settings.local.json` is not loaded by
the SDK (no `setting_sources`), and its `Read(/datadir/secrets/**)` was
relative to the file anyway (an absolute path is `//path` in that syntax). The
real boundary is the spawn's uid against root-only directories — and on a
macOS host `/datadir` is a virtiofs bind where a 700 root directory was still
traversable by uid 60000.

So, one more wall, inside the agent-server and not configurable: the
permission gate refuses the file tools (`Read`, `Write`, `Edit`, `MultiEdit`,
`NotebookEdit`, `Grep`, `Glob`) on these paths, resolved with symlinks
followed. It is defence in depth for the file tools, not a shell sandbox:
`Bash` stays behind the uid, which is what `self_test` checks at startup.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import subprocess
import sys

LOG = logging.getLogger("agent-server.file_guard")

#: Directories no spawn reads or writes, whatever its seed says.
PROTECTED_DIRS: tuple[str, ...] = (
    "/datadir/secrets",
    "/datadir/clodia-vault",
    "/clodia/secrets",
    "/platform-src",
)
#: Under this root, files that look like credentials.
BUNDLE_ROOT = "/clodia"
SECRET_FILE_PATTERNS: tuple[str, ...] = (".env*", "*.key")

#: The input keys that carry a path, per tool.
PATH_KEYS: dict[str, tuple[str, ...]] = {
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Grep": ("path",),
    "Glob": ("path", "pattern"),
}
#: Tools that walk a tree: their root must not CONTAIN a protected directory.
SEARCH_TOOLS = frozenset({"Grep", "Glob"})

_WILDCARDS = "*?[{"


def _norm(p: str) -> str:
    return os.path.normpath(p)


def _real(p: str) -> str:
    try:
        return os.path.realpath(p)
    except (OSError, ValueError):
        return _norm(p)


def _protected() -> list[str]:
    """The protected directories, both as written and as resolved: on a host
    where one of them is a symlink, either spelling must hit."""
    out: list[str] = []
    for d in PROTECTED_DIRS:
        for v in (_norm(d), _real(d)):
            if v not in out:
                out.append(v)
    return out


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _literal_prefix(pattern: str) -> str:
    """`/datadir/secrets/**/*.json` → `/datadir/secrets`: the part of a glob
    before its first wildcard, cut at a path separator."""
    cut = min((pattern.find(c) for c in _WILDCARDS if c in pattern), default=-1)
    if cut < 0:
        return pattern
    return pattern[:cut].rsplit("/", 1)[0] or "/"


def _paths_of(tool: str, tool_input: dict, cwd: str | None) -> list[str]:
    out: list[str] = []
    for key in PATH_KEYS.get(tool, ()):
        v = (tool_input or {}).get(key)
        if not isinstance(v, str) or not v.strip():
            continue
        v = v.strip()
        if key == "pattern":
            if not v.startswith("/"):
                continue          # relative glob: it lives under `path`/cwd
            v = _literal_prefix(v)
        if not os.path.isabs(v):
            if not cwd:
                continue          # relative to a cwd nobody told us: not guessed
            v = os.path.join(cwd, v)
        out.append(v)
    if tool in SEARCH_TOOLS and cwd and not (tool_input or {}).get("path"):
        out.append(cwd)           # the search runs from the spawn's cwd
    return out


def refused(tool: str, tool_input: dict, cwd: str | None = None) -> str | None:
    """Why this call touches a protected path, or None."""
    if tool not in PATH_KEYS:
        return None
    protected = _protected()
    bundle = {_norm(BUNDLE_ROOT), _real(BUNDLE_ROOT)}
    for raw in _paths_of(tool, tool_input, cwd):
        for p in {_norm(raw), _real(raw)}:
            for d in protected:
                if _within(p, d):
                    return f"{p} è in un'area protetta della piattaforma"
                if tool in SEARCH_TOOLS and _within(d, p):
                    return f"la ricerca in {p} attraverserebbe {d}, area protetta"
            if any(_within(p, b) for b in bundle):
                name = os.path.basename(p)
                if any(fnmatch.fnmatchcase(name, pat) for pat in SECRET_FILE_PATTERNS):
                    return f"{p} ha la forma di una credenziale"
    return None


_PROBE = r"""
import os, sys
for p in sys.argv[1:]:
    try:
        if os.path.isdir(p):
            os.listdir(p)
        else:
            open(p, "rb").close()
    except OSError:
        continue
    print(p)
"""


def self_test(uid: int, paths: tuple[str, ...] = PROTECTED_DIRS) -> list[str]:
    """As the sandbox uid, try each protected path: the ones it CAN read.

    The proposal's P3: the boundary that actually holds is the uid, and on a
    virtiofs bind it has failed once. A non-empty answer is logged as an error
    at startup. Paths that do not exist are skipped. Needs root (to switch
    uid); otherwise nothing is probed and the answer is empty."""
    if not uid or os.getuid() != 0:
        return []
    existing = [p for p in paths if os.path.exists(p)]
    if not existing:
        return []
    try:
        r = subprocess.run([sys.executable, "-c", _PROBE, *existing], user=uid,
                           capture_output=True, text=True, timeout=10)
    except Exception as e:  # noqa: BLE001 - a failed probe is reported, never fatal
        LOG.warning("autotest delle aree protette non eseguito (%s)", type(e).__name__)
        return []
    readable = [line for line in r.stdout.splitlines() if line.strip()]
    if readable:
        LOG.error("uid %s del sandbox LEGGE aree protette della piattaforma: %s "
                  "(clodia-platform#665: controllare i permessi del volume)",
                  uid, ", ".join(readable))
    else:
        LOG.info("autotest: uid %s del sandbox non legge nessuna area protetta", uid)
    return readable
