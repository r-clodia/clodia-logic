"""Pagina di decisione dei gate via link firmato — SENZA login.

Il token (gate_sign) autorizza la sola decisione di UN gate specifico ed è
one-time (il nonce deve combaciare con quello salvato sul run; risolto il gate
il nonce sparisce → link morto). Nessun'altra operazione è possibile con esso.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import gate_sign
from ..workflows import engine, store

LOG = logging.getLogger("agent-server.api.gate_public")
router = APIRouter()


# ---------------------------------------------------------------------------
# Gate delle PROPOSTE DI JOB (stessa pagina/token del gate workflow, kind="job")
# ---------------------------------------------------------------------------

def _resolve_job(token: str) -> dict:
    payload = gate_sign.verify_job(token)
    if not payload:
        raise HTTPException(403, "link non valido o scaduto")
    from ..scheduler import proposals
    prop = proposals.get(payload["job"])
    if not prop:
        raise HTTPException(404, "proposta non trovata")
    if prop.get("status") != "pending" or not prop.get("nonce"):
        raise HTTPException(409, "questa proposta non è più in attesa")
    if prop.get("nonce") != payload["nonce"]:
        raise HTTPException(403, "link già usato o non più valido")
    return prop


def _job_view(prop: dict) -> dict:
    """Stessa shape del gate workflow → la pagina /gate la rende senza modifiche."""
    sched = prop.get("cron_expr") or "—"
    summary = (f"Agente al fire: {prop.get('agent')}\n"
               f"Schedule (cron): {sched}\n"
               f"Abilitato: {'sì' if prop.get('enabled', True) else 'no'}\n\n"
               f"Prompt del job:\n{prop.get('prompt', '')}")
    return {
        "run_id": f"job:{prop['id']}",
        "title": prop.get("name", ""),
        "workflow": f"Proposta di job (da {prop.get('requested_by') or 'agente'})",
        "lane": f"schedule {sched}",
        "summary": summary,
        "artefatto": None,
        "choices": ["Approva", "Annulla"],
    }


def _job_decide(prop: dict, choice: str, comment: str) -> dict:
    from ..scheduler import db, proposals, scheduler
    if choice.startswith("approva"):
        proposals.resolve(prop["id"], "approved", comment)
        import sqlite3
        try:
            job = db.create_job(
                name=prop["name"], cron_expr=prop["cron_expr"],
                prompt=prop["prompt"], agent=prop["agent"],
                enabled=bool(prop.get("enabled", True)))
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"esiste già un job con nome '{prop['name']}'")
        if job.get("enabled"):
            try:
                scheduler.register_job(job)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(500, f"job creato (id={job['id']}) ma "
                                         f"registrazione scheduler fallita: {e}")
        return {"ok": True, "outcome": "approvato", "job_id": job["id"]}
    proposals.resolve(prop["id"], "rejected", comment)
    return {"ok": True, "outcome": "annullato"}


def _resolve(token: str) -> dict:
    """Verifica firma + scadenza + one-time (nonce), ritorna il run in gate."""
    payload = gate_sign.verify(token)
    if not payload:
        raise HTTPException(403, "link non valido o scaduto")
    run = store.load_run(payload["run"])
    if not run:
        raise HTTPException(404, "run non trovato")
    if run.get("status") != "await" or not run.get("gate_pending"):
        raise HTTPException(409, "questo gate non è più in attesa")
    if run.get("current") != payload["stage"] or run.get("gate_nonce") != payload["nonce"]:
        raise HTTPException(403, "link già usato o non più valido")
    return run


@router.get("/gate/{token}")
async def gate_view(token: str) -> dict:
    """Dati per la pagina di decisione (no login). Kind workflow o job."""
    if gate_sign.token_kind(token) == "job":
        return _job_view(_resolve_job(token))
    run = _resolve(token)
    idx = run["current"]
    stage = run["stages"][idx]
    hist = next((h for h in reversed(run["history"])
                 if h.get("stage_idx") == idx and h.get("status") == "ok"), None)
    return {
        "run_id": run["id"],
        "title": run["title"],
        "workflow": f"{run['plugin']}/{run['workflow']}",
        "lane": stage["lane"],
        "summary": (hist or {}).get("summary", ""),
        "artefatto": (hist or {}).get("artefatto"),
        "choices": ["Approva", "Rimanda con modifiche", "Annulla"],
    }


class Decide(BaseModel):
    choice: str
    comment: str = ""


@router.post("/gate/{token}/decide")
async def gate_decide(token: str, body: Decide) -> dict:
    """Applica la decisione (one-time): consuma il nonce e risolve il gate."""
    if gate_sign.token_kind(token) == "job":
        prop = _resolve_job(token)
        return _job_decide(prop, (body.choice or "").strip().lower(), body.comment)
    run = _resolve(token)
    idx = run["current"]
    choice = (body.choice or "").strip().lower()
    who = run.get("wf_owner") or "owner"
    # consuma il nonce PRIMA di risolvere → il link non è più riusabile
    run["gate_nonce"] = None
    store.save_run(run)
    try:
        if choice.startswith("approva"):
            engine.approve(run["id"], who, body.comment)
            outcome = "approvato"
        elif choice.startswith("annulla"):
            await engine.cancel(run["id"], who, body.comment)
            outcome = "annullato"
        else:  # rimanda con modifiche
            engine.reject(run["id"], who, body.comment or "rimanda")
            outcome = "rimandato per modifiche"
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "outcome": outcome}
