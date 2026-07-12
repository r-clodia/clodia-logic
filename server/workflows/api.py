"""API dei workflow dichiarativi (montata solo con features.kanban)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..api.agents import _principal_from_request
from . import engine, store

router = APIRouter()


def _require_login(request: Request) -> str:
    principal = _principal_from_request(request)
    if not principal:
        raise HTTPException(401, "login richiesto")
    return principal


@router.get("/clodia/workflows")
async def list_workflows(request: Request) -> dict:
    _require_login(request)
    # i run cancellati spariscono dalla pagina (i file restano per audit)
    runs = [r for r in store.list_runs() if r.get("status") != "cancelled"]
    # per i run in await: allega la domanda corrente (inline sulla board)
    for r in runs:
        if r.get("status") == "await":
            r["question"] = engine.current_question(r)
    return {"workflows": store.available_workflows(), "runs": runs}


class StartBody(BaseModel):
    title: str = ""
    params: str = ""
    topic: dict | None = None      # {tier, name} opzionale


@router.post("/clodia/workflows/{plugin}/{name}/start")
async def start_workflow(plugin: str, name: str, body: StartBody, request: Request) -> dict:
    principal = _require_login(request)
    try:
        run = store.create_run(plugin, name, title=body.title, params=body.params,
                               topic=None, requested_by=principal)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Crea subito il topic effimero del run (participants = agenti + umano),
    # così la risposta porta già il link alla conversazione.
    run = engine.ensure_topic(run)
    return run


@router.get("/clodia/workflows/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict:
    _require_login(request)
    try:
        run = store.load_run(run_id)
    except ValueError:
        raise HTTPException(400, "run id non valido")
    if not run:
        raise HTTPException(404, "run non trovato")
    return run


class VerdictBody(BaseModel):
    note: str = ""


class AnswerBody(BaseModel):
    text: str = ""


@router.post("/clodia/workflows/runs/{run_id}/answer")
async def answer_run(run_id: str, body: AnswerBody, request: Request) -> dict:
    """Risposta inline dalla board a una domanda dell'agente (intake o gate)."""
    principal = _require_login(request)
    if not body.text.strip():
        raise HTTPException(400, "testo vuoto")
    try:
        return await engine.submit_answer(run_id, principal, body.text.strip())
    except KeyError:
        raise HTTPException(404, "run non trovato")
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/clodia/workflows/runs/{run_id}/approve")
async def approve_run(run_id: str, body: VerdictBody, request: Request) -> dict:
    principal = _require_login(request)
    try:
        return engine.approve(run_id, principal, body.note)
    except KeyError:
        raise HTTPException(404, "run non trovato")
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/clodia/workflows/runs/{run_id}/cancel")
async def cancel_run(run_id: str, body: VerdictBody, request: Request) -> dict:
    principal = _require_login(request)
    try:
        return await engine.cancel(run_id, principal, body.note)
    except KeyError:
        raise HTTPException(404, "run non trovato")
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.delete("/clodia/workflows/runs/{run_id}")
async def delete_run_endpoint(run_id: str, request: Request) -> dict:
    principal = _require_login(request)
    try:
        removed = await engine.delete(run_id, principal)
    except ValueError:
        raise HTTPException(400, "run id non valido")
    if not removed:
        raise HTTPException(404, "run non trovato")
    return {"ok": True, "deleted": run_id}


@router.post("/clodia/workflows/runs/{run_id}/reject")
async def reject_run(run_id: str, body: VerdictBody, request: Request) -> dict:
    principal = _require_login(request)
    try:
        return engine.reject(run_id, principal, body.note)
    except KeyError:
        raise HTTPException(404, "run non trovato")
    except ValueError as e:
        raise HTTPException(409, str(e))
