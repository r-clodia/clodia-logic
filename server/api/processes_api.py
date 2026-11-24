"""Processes in the UI (clodia-platform#738): the courier, the topic indexer
and the logic jobs (the backup among them), for the Jobs and Activity pages.

Admin only. A process has no owner — the backup job is ownerless already, and
the courier reads every room's mail — so there is no narrower view to give a
non-admin: what it would see is the colony's machinery, not its own work.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from .. import processes

router = APIRouter(tags=["processes"])


def _require_admin(request: Request) -> str:
    from . import caller_auth
    from .admin import is_admin, is_human
    who = caller_auth.caller_principal(request)
    if not who:
        raise HTTPException(401, "autenticazione richiesta")
    if not (is_admin(who) and is_human(who)):
        raise HTTPException(403, "solo un admin vede i processi")
    return who


def _jobs() -> list[dict]:
    from ..scheduler import db
    return db.list_jobs()


def _next_fire(job_id: int):
    from ..scheduler import scheduler
    return scheduler.next_fire(job_id)


@router.get("/api/processes")
async def list_processes(request: Request) -> dict:
    _require_admin(request)
    jobs = await asyncio.to_thread(_jobs)
    rows = await asyncio.to_thread(processes.list_processes, jobs, _next_fire)
    return {"windows": list(processes.WINDOWS), "processes": rows}


@router.get("/api/processes/{name}/runs")
async def process_runs(name: str, request: Request, limit: int = 50) -> dict:
    """The last passes of a loop process. A logic job's history is the job's
    own (`GET /clodia/jobs/{id}`), not repeated here."""
    _require_admin(request)
    if processes.get(name) is None:
        raise HTTPException(404, f"processo '{name}' non trovato")
    limit = max(1, min(int(limit), 500))
    return {"name": name,
            "runs": await asyncio.to_thread(processes.loop_history, name, limit)}


@router.post("/api/processes/{name}/run")
async def run_process(name: str, request: Request) -> dict:
    """One pass now, on the process's own loop: the request wakes it, it does
    not run a second pass beside it."""
    _require_admin(request)
    p = processes.get(name)
    if p is None:
        raise HTTPException(404, f"processo '{name}' non trovato")
    if not p.enabled:
        raise HTTPException(409, f"il processo '{name}' è spento")
    processes.request_run(name)
    return {"name": name, "requested": True}
