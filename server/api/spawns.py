"""API proc-like degli spawn degli agent.

Modello "/spawns" (come /proc di Linux): ogni spawn vivo è una cartella
`clodia-data/spawns/<name>-<n>` che materializza seed+stato di un agent + uno
scratch. Questo endpoint la espone in sola lettura per la UI/observability.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from ..agents.workspace import SPAWNS_ROOT

router = APIRouter()


@router.get("/api/spawns")
async def list_spawns() -> dict:
    """Elenca gli spawn attualmente materializzati sotto clodia-data/spawns/."""
    out: list[dict] = []
    if SPAWNS_ROOT.is_dir():
        for d in sorted(SPAWNS_ROOT.iterdir()):
            if not d.is_dir():
                continue
            # <name>-<instance> (instance tipicamente numerico, proc-like)
            agent, sep, instance = d.name.rpartition("-")
            if not sep:
                agent, instance = d.name, ""
            try:
                mtime = datetime.fromtimestamp(d.stat().st_mtime, timezone.utc).isoformat()
            except OSError:
                mtime = None
            out.append({
                "id": d.name,
                "agent": agent,
                "instance": instance,
                "has_scratch": (d / "scratch").is_dir(),
                "last_activity": mtime,
            })
    return {"spawns": out, "root": str(SPAWNS_ROOT)}
