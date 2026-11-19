"""The pack catalogue: every public pack, with its state on this instance
(clodia-platform#700).

One list for the Packs page, from two sources:

- **clodia-packs** — every `packs/<name>/pack.yaml` of `r-clodia/clodia-packs`
  at `main`, read through the GitHub API (vault PAT when present) and cached
  for `TTL` seconds;
- **first-party** — the packs bundled with this agent-server
  (`catalogs/packs/<name>`), whose upstream is `r-clodia/clodia-logic`.

Each entry carries a `state`:

    installed               installed, catalogue version not newer
    update_available        installed, catalogue version newer
    available               in the catalogue, not installed
    deprecated              the catalogue (or the installed manifest) says so;
                            `installed` tells whether it is here
    removed_from_catalogue  installed from one of the two catalogues, but no
                            longer in it (its upstream path is gone)

A pack installed from anywhere else (its own repository, a zip, a local
folder) is `installed` with `source: other`: absent from the catalogue is not
removed from it.

`deprecated` in a `pack.yaml` is `true` or `{reason, replacement, since}`;
`deprecation()` normalises both. A deprecated pack that is not installed is
shown but not installable (owner, 18 Nov 2026); an installed one keeps working.

A GitHub failure never empties the page: the installed packs are still
listed, and `catalogue_error` says why the rest is missing.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import yaml

LOG = logging.getLogger("agent-server.api.pack_catalogue")

#: The public catalogue repository and the directory of its packs.
REPO = "r-clodia/clodia-packs"
REF = "main"
DIR = "packs"
#: The first-party upstream (bundled catalogue).
FIRST_PARTY_REPO = "r-clodia/clodia-logic"
FIRST_PARTY_DIR = "catalogs/packs"
#: Seconds a catalogue read is reused.
TTL = 300.0

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"at": 0.0, "packs": None, "error": ""}


def deprecation(manifest: dict | None) -> dict | None:
    """`{reason, replacement, since}` when `manifest` declares the pack
    deprecated (`deprecated: true` or a mapping), else None."""
    raw = (manifest or {}).get("deprecated") if isinstance(manifest, dict) else None
    if raw is True:
        return {"reason": "", "replacement": "", "since": ""}
    if isinstance(raw, dict):
        return {k: str(raw.get(k) or "").strip() for k in ("reason", "replacement", "since")}
    return None


def _version_tuple(v: str):
    from .packs import _version_tuple as vt
    return vt(v)


def newer(a: str, b: str) -> bool:
    """`a` is a newer version than `b` (semver-ish, as check-update)."""
    if not a:
        return False
    try:
        return _version_tuple(a) > _version_tuple(b or "0")
    except TypeError:          # a non-numeric version on one side
        return a != b


# ── reading the catalogue ──────────────────────────────────────────────────

def _gh_get(path: str, *, raw: bool = False):
    import httpx
    from .packs import _github_token
    headers = {"Accept": "application/vnd.github.raw+json" if raw
               else "application/vnd.github+json"}
    tok = _github_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    r = httpx.get(f"https://api.github.com/repos/{REPO}/contents/{path}",
                  params={"ref": REF}, headers=headers, timeout=12.0)
    r.raise_for_status()
    return r


def _fetch_clodia_packs() -> dict[str, dict]:
    """`{name: manifest}` of every pack of the public catalogue."""
    from . import catalog
    out: dict[str, dict] = {}
    for row in _gh_get(DIR).json() or []:
        name = str((row or {}).get("name") or "")
        if (row or {}).get("type") != "dir" or not catalog._NAME_RE.fullmatch(name):
            continue
        try:
            text = _gh_get(f"{DIR}/{name}/pack.yaml", raw=True).text
            m = yaml.safe_load(text) or {}
        except Exception as e:  # noqa: BLE001 — one unreadable pack is skipped
            LOG.warning("catalogue: %s/%s/pack.yaml unreadable (%s)", DIR, name,
                        type(e).__name__)
            continue
        if isinstance(m, dict):
            out[name] = m
    return out


def clodia_packs(force: bool = False) -> tuple[dict[str, dict], str]:
    """The public catalogue, cached: `({name: manifest}, error)`. On a failed
    read the last good copy is served, with the error."""
    with _LOCK:
        fresh = time.monotonic() - _CACHE["at"] < TTL
        if _CACHE["packs"] is not None and fresh and not force:
            return dict(_CACHE["packs"]), _CACHE["error"]
    try:
        packs, err = _fetch_clodia_packs(), ""
    except Exception as e:  # noqa: BLE001 — the page renders anyway
        LOG.warning("catalogue: %s unreadable (%s)", REPO, e)
        packs, err = None, f"catalogo {REPO} non raggiungibile ({type(e).__name__})"
    with _LOCK:
        if packs is not None:
            _CACHE.update(at=time.monotonic(), packs=packs, error="")
        else:
            _CACHE.update(at=time.monotonic(), error=err)
        return dict(_CACHE["packs"] or {}), _CACHE["error"]


def clear_cache() -> None:
    with _LOCK:
        _CACHE.update(at=0.0, packs=None, error="")


def _bundled() -> dict[str, dict]:
    from ..config import workspace_path
    from . import catalog
    root = workspace_path(FIRST_PARTY_DIR)
    out: dict[str, dict] = {}
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not catalog._NAME_RE.fullmatch(d.name):
            continue
        try:
            m = yaml.safe_load((d / "pack.yaml").read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        if isinstance(m, dict):
            out[d.name] = m
    return out


def _installed() -> dict[str, dict]:
    from . import pack_import
    root = pack_import.PACKS_META_DIR
    out: dict[str, dict] = {}
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        p = d / "pack.yaml"
        if not d.is_dir() or not p.is_file():
            continue
        try:
            m = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            m = {}
        out[d.name] = m if isinstance(m, dict) else {}
    return out


def _upstream(m: dict) -> dict:
    up = (m or {}).get("upstream")
    if not isinstance(up, dict) or not up.get("repo"):
        return {}
    return {"repo": str(up["repo"]).strip(),
            "path": str(up.get("path") or "").strip().strip("/"),
            "ref": str(up.get("ref") or "main").strip()}


def _from_catalogue(up: dict, name: str) -> str:
    """Which catalogue an installed pack came from, judging by its upstream:
    `clodia-packs`, `first-party`, or "" (somewhere else)."""
    repo, path = up.get("repo", ""), up.get("path", "")
    if repo == REPO and path.startswith(f"{DIR}/"):
        return "clodia-packs"
    if repo == FIRST_PARTY_REPO and path.startswith(f"{FIRST_PARTY_DIR}/"):
        return "first-party"
    return ""


# ── the list ───────────────────────────────────────────────────────────────

def entries(force: bool = False) -> dict[str, Any]:
    """`{packs: [...], catalogue_error}` — see the module docstring."""
    public, err = clodia_packs(force)
    bundled = _bundled()
    installed = _installed()
    out: list[dict] = []

    def row(name: str, cat_m: dict | None, source: str, up: dict) -> dict:
        inst_m = installed.get(name)
        cat_v = str((cat_m or {}).get("version") or "").strip()
        inst_v = str((inst_m or {}).get("version") or "").strip() if inst_m is not None else ""
        dep = deprecation(cat_m) or deprecation(inst_m)
        is_inst = inst_m is not None
        upd = is_inst and bool(cat_m) and newer(cat_v, inst_v)
        if cat_m is None and is_inst:
            state = "removed_from_catalogue" if source in ("clodia-packs", "first-party") \
                else "installed"
        elif dep:
            state = "deprecated"
        elif not is_inst:
            state = "available"
        else:
            state = "update_available" if upd else "installed"
        desc = str(((cat_m if cat_m is not None else inst_m) or {}).get("description")
                   or "").strip()
        return {"name": name, "description": desc,
                "version": cat_v, "installed_version": inst_v,
                "installed": is_inst, "update_available": upd,
                "source": source or "other", "upstream": up,
                "deprecated": dep, "state": state,
                "installable": not is_inst and cat_m is not None and not dep}

    for name, m in sorted(public.items()):
        out.append(row(name, m, "clodia-packs",
                       {"repo": REPO, "path": f"{DIR}/{name}", "ref": REF}))
    for name, m in sorted(bundled.items()):
        if name in public:
            continue
        out.append(row(name, m, "first-party",
                       _upstream(m) or {"repo": FIRST_PARTY_REPO,
                                        "path": f"{FIRST_PARTY_DIR}/{name}", "ref": "main"}))
    seen = {r["name"] for r in out}
    for name, m in sorted(installed.items()):
        if name in seen:
            continue
        up = _upstream(m)
        src = _from_catalogue(up, name)
        if src == "clodia-packs" and err and not public:
            # The catalogue could not be read: not knowing is not «removed».
            src = "unknown"
        out.append(row(name, None, src, up))
    return {"packs": out, "catalogue_error": err}


def entry(name: str, force: bool = False) -> dict | None:
    for r in entries(force)["packs"]:
        if r["name"] == name:
            return r
    return None
