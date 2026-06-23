"""REST API per gestione job dello scheduler.

Endpoint montati su `/clodia/jobs`:
  GET    /clodia/jobs           → lista job
  GET    /clodia/jobs/{id}      → singolo
  POST   /clodia/jobs           → crea (+ registra in APScheduler se enabled)
  PATCH  /clodia/jobs/{id}      → update (+ ricarica in APScheduler)
  DELETE /clodia/jobs/{id}      → rimuove (+ deregistra)
  POST   /clodia/jobs/{id}/run  → fire immediato manuale

Validazione cron: rifiuta espressioni invalide con 422.
"""
import sqlite3
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import db, scheduler, nl_schedule
from ..sdk_runtime.session import available_kinds, known_kind, provider_connected_for

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemi I/O
# ---------------------------------------------------------------------------

class JobCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    # cron a 5 campi OPPURE schedule_text in linguaggio naturale (uno dei due).
    cron_expr: Optional[str] = Field(None, max_length=200)
    schedule_text: Optional[str] = Field(None, max_length=200)
    prompt: str = Field(..., min_length=1)
    # Agent (kind) che lo scheduler spawna al fire. Default "clodia" (superficie
    # pristine). Risolto dinamicamente: kind statico o agent del registry.
    agent: str = Field("clodia", min_length=1, max_length=100)
    enabled: bool = True


class JobUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    cron_expr: Optional[str] = Field(None, min_length=1, max_length=200)
    schedule_text: Optional[str] = Field(None, max_length=200)
    prompt: Optional[str] = None
    agent: Optional[str] = Field(None, min_length=1, max_length=100)
    enabled: Optional[bool] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_valid_cron(expr: str) -> None:
    err = scheduler.validate_cron_expr(expr)
    if err:
        raise HTTPException(status_code=422, detail=f"invalid cron_expr: {err}")


def _resolve_cron(cron_expr: Optional[str], schedule_text: Optional[str]) -> str:
    """Ritorna un cron valido da cron_expr (prioritario) o dal linguaggio naturale."""
    if cron_expr and cron_expr.strip():
        _require_valid_cron(cron_expr)
        return cron_expr.strip()
    if schedule_text and schedule_text.strip():
        try:
            cron, _desc = nl_schedule.parse(schedule_text)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        _require_valid_cron(cron)
        return cron
    raise HTTPException(status_code=422, detail="serve cron_expr oppure schedule_text")


@router.get("/clodia/jobs/parse-schedule")
async def api_parse_schedule(text: str):
    """Anteprima: linguaggio naturale → cron + descrizione (per la webui)."""
    try:
        cron, desc = nl_schedule.parse(text)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"cron_expr": cron, "description": desc}


def _require_valid_agent(agent: str) -> None:
    if not known_kind(agent):
        raise HTTPException(
            status_code=422,
            detail=f"unknown agent '{agent}'; available: {available_kinds()}",
        )
    # Enforcement: non schedulare un job per un agent col provider scollegato —
    # non sarebbe disponibile al fire. Collegare il provider prima.
    if not provider_connected_for(agent):
        raise HTTPException(
            status_code=409,
            detail=f"agent '{agent}': provider non collegato — "
                   f"collega il provider dalla sezione Providers prima di schedulare un job",
        )


def _require_job(job_id: int) -> dict:
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return job


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/clodia/jobs")
async def api_list_jobs():
    return db.list_jobs()


@router.get("/clodia/jobs/{job_id}")
async def api_get_job(job_id: int):
    return _require_job(job_id)


@router.post("/clodia/jobs", status_code=201)
async def api_create_job(req: JobCreate):
    cron = _resolve_cron(req.cron_expr, req.schedule_text)
    _require_valid_agent(req.agent)
    try:
        job = db.create_job(
            name=req.name,
            cron_expr=cron,
            prompt=req.prompt,
            agent=req.agent,
            enabled=req.enabled,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail=f"job name '{req.name}' already exists")
    if job["enabled"]:
        try:
            scheduler.register_job(job)
        except Exception as e:
            # Job creato sul DB ma non registrato — situazione anomala, segnaliamo.
            raise HTTPException(
                status_code=500,
                detail=f"job created (id={job['id']}) but scheduler registration failed: {e}",
            )
    return job


@router.patch("/clodia/jobs/{job_id}")
async def api_update_job(job_id: int, req: JobUpdate):
    _require_job(job_id)
    cron = req.cron_expr
    if cron is None and req.schedule_text:
        cron = _resolve_cron(None, req.schedule_text)
    elif cron is not None:
        _require_valid_cron(cron)
    if req.agent is not None:
        _require_valid_agent(req.agent)
    try:
        updated = db.update_job(
            job_id,
            name=req.name,
            cron_expr=cron,
            prompt=req.prompt,
            agent=req.agent,
            enabled=req.enabled,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail=f"job name '{req.name}' already exists")
    if updated is None:  # race: cancellato fra _require_job e update
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    # Sincronizza APScheduler: se enabled → registra (replace), altrimenti deregistra.
    if updated["enabled"]:
        scheduler.register_job(updated)
    else:
        scheduler.unregister_job(job_id)
    return updated


@router.delete("/clodia/jobs/{job_id}")
async def api_delete_job(job_id: int):
    _require_job(job_id)
    scheduler.unregister_job(job_id)
    deleted = db.delete_job(job_id)
    if not deleted:  # race
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return {"deleted": job_id}


@router.post("/clodia/jobs/{job_id}/run")
async def api_run_job(job_id: int):
    """Fire manuale immediato (bypassa il cron). Utile per test e debug."""
    _require_job(job_id)
    result = await scheduler.fire_job(job_id)
    return {"job_id": job_id, **result}
