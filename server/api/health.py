import asyncio
import subprocess
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter

from .. import PLATFORM_VERSION, __version__
from ..config import WORKSPACE_ROOT

router = APIRouter()

_REPO_ROOT = WORKSPACE_ROOT  # root del repo clodia-logic (git rev-parse del commit)
_COMMIT_CACHE: dict = {"sha": "unknown", "expires": 0.0}
_COMMIT_TTL_SECONDS = 5  # cache breve così i nuovi commit appaiono in pochi secondi


def _resolve_commit_short() -> str:
    now = time.time()
    if now < _COMMIT_CACHE["expires"]:
        return _COMMIT_CACHE["sha"]
    sha = "unknown"
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0:
            sha = r.stdout.strip()
    except Exception:
        pass
    _COMMIT_CACHE["sha"] = sha
    _COMMIT_CACHE["expires"] = now + _COMMIT_TTL_SECONDS
    return sha


_PLATFORM_CACHE: dict = {}


def _resolve_platform_tag() -> str:
    """Tag collettivo di piattaforma: dal git se raggiungibile, altrimenti la
    costante dichiarata.

    L'ordine conta. Il tag del tree deployato è un FATTO; la costante è una
    dichiarazione, e una dichiarazione può essere rimasta indietro. Dove i tag
    non ci sono — il deploy clona con `--depth=1`, quindi oggi è il caso — si
    ripiega sulla costante, che è comunque un posto solo invece di uno per repo.
    """
    if "value" in _PLATFORM_CACHE:
        return _PLATFORM_CACHE["value"]
    tag = ""
    try:
        out = subprocess.run(["git", "describe", "--tags", "--abbrev=0"],
                             cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            tag = (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        tag = ""
    value = tag or f"v{PLATFORM_VERSION}"
    _PLATFORM_CACHE["value"] = value
    return value


@router.get("/health")
async def health():
    # `_resolve_commit_short()` fa un `subprocess.run(git …)` bloccante: chiamarlo
    # direttamente nell'event loop può appendere /health (interazione fra il
    # waitpid di subprocess e il child-watcher asyncio, con molti subprocess
    # figli). Lo eseguiamo in un thread → il loop non si blocca mai e
    # l'healthcheck non va in timeout.
    commit = await asyncio.to_thread(_resolve_commit_short)
    return {
        "status": "ok",
        "version": __version__,
        # `platform` = tag collettivo, `version` = semver di QUESTO componente.
        # La webui mostra il primo e tiene il secondo nel title: un badge che
        # dichiara una release mentre gira codice successivo non è verificabile,
        # e affiancare ciò che sta davvero girando lo rende onesto.
        "platform": await asyncio.to_thread(_resolve_platform_tag),
        "commit": commit,
        "timestamp": datetime.utcnow().isoformat(),
    }
