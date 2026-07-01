import asyncio
import subprocess
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter

from .. import __version__
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
        "commit": commit,
        "timestamp": datetime.utcnow().isoformat(),
    }
