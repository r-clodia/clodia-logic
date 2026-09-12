"""Pagina di decisione dei gate via link firmato — SENZA login.

Il token (gate_sign) autorizza la sola decisione di UN gate specifico ed è
one-time (il nonce deve combaciare con quello salvato sulla proposta; risolto
il gate il nonce sparisce → link morto). Nessun'altra operazione è possibile.

Questo modulo serviva DUE cose con una pagina sola, finché l'engine rimosso il
9 ago 2026 è stato l'altra: l'approvazione via link di un job, che è viva,
dipendeva così da un flag di feature spento ovunque l'abbia misurato, e la
pagina restava raggiungibile solo per caso.

Ora è montata sempre e parla solo di job. Se arriva un token di quelli vecchi
la risposta è «non valido», che è la verità: ciò cui si riferiva non esiste più.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import gate_sign

LOG = logging.getLogger("agent-server.api.gate_public")
router = APIRouter()


# ---------------------------------------------------------------------------
# Gate delle PROPOSTE DI JOB
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
    """Shape attesa dalla pagina /gate, che la rende senza modifiche.

    I nomi dei campi (`run_id`, `"workflow"`, `lane`) sono ereditati e restano:
    sono il CONTRATTO verso un consumatore che da qui non si vede, e rinominarli
    romperebbe la pagina per guadagnare solo un lessico più pulito. Il valore,
    quello sì, dice la cosa vera — «Proposta di job».
    """
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
    from ..scheduler import proposals
    try:
        return proposals.apply_decision(prop, choice, comment)
    except proposals.ProposalError as e:
        raise HTTPException(e.status, e.detail)


@router.get("/gate/{token}")
async def gate_view(token: str) -> dict:
    """Dati per la pagina di decisione (no login)."""
    return _job_view(_resolve_job(token))


class Decide(BaseModel):
    choice: str
    comment: str = ""


@router.post("/gate/{token}/decide")
async def gate_decide(token: str, body: Decide) -> dict:
    """Applica la decisione (one-time): consuma il nonce e risolve il gate."""
    prop = _resolve_job(token)
    return _job_decide(prop, (body.choice or "").strip().lower(), body.comment)
