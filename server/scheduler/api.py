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
import asyncio
import sqlite3
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import db, scheduler, nl_schedule
from ..api import topics_client
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
    # Tier dei DATI che il job tratterà. Vuoto = non dichiarato = nessun
    # requisito. Se dichiarato, al fire l'agente deve girare su un provider di
    # SEAL almeno pari, altrimenti il run fallisce (voce 13: la SEAL effettiva è
    # quella del provider, perché è lì che il dato va).
    tier: Optional[str] = Field(None, max_length=10)


class JobPropose(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    cron_expr: Optional[str] = Field(None, max_length=200)
    schedule_text: Optional[str] = Field(None, max_length=200)
    prompt: str = Field(..., min_length=1)
    agent: str = Field("clodia", min_length=1, max_length=100)
    enabled: bool = True
    # chi propone (impostato dal gateway dall'identità dell'agente chiamante)
    requested_by: str = Field("", max_length=100)


class JobUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    cron_expr: Optional[str] = Field(None, min_length=1, max_length=200)
    schedule_text: Optional[str] = Field(None, max_length=200)
    prompt: Optional[str] = None
    agent: Optional[str] = Field(None, min_length=1, max_length=100)
    enabled: Optional[bool] = None
    # Tier dei DATI che il job tratterà. Vuoto = non dichiarato = nessun
    # requisito. Se dichiarato, al fire l'agente deve girare su un provider di
    # SEAL almeno pari, altrimenti il run fallisce (voce 13: la SEAL effettiva è
    # quella del provider, perché è lì che il dato va).
    tier: Optional[str] = Field(None, max_length=10)



class TopicCronTriggerUpsert(BaseModel):
    """Periodicità del trigger di un topic: «ogni N minuti, per M volte» (#239).

    Ha sostituito il `cron_expr` a testo libero. `repeat_count = 0` = ricorrente
    senza fine (il caso che il cron copriva e che resta necessario ai trigger di
    sorveglianza)."""
    interval_minutes: int = Field(..., ge=1)
    repeat_count: int = Field(0, ge=0)
    prompt: str = Field(..., min_length=1)
    agent: Optional[str] = Field(None, max_length=100)
    # Un vecchio client che manda ancora il cron non deve vederselo IGNORARE in
    # silenzio (creerebbe un trigger con una cadenza che nessuno ha chiesto):
    # il campo è dichiarato apposta per poterlo rifiutare con un motivo.
    cron_expr: Optional[str] = Field(None, max_length=200)


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
    if job is None or job.get("mode") == "topic_trigger":
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return job


def _caller(request: Request) -> str:
    from ..api.agents import _principal_from_request
    principal = _principal_from_request(request)
    if not principal:
        raise HTTPException(401, "autenticazione richiesta")
    return principal


def _require_job_owner(request: Request, job: dict) -> str:
    """Agire su un job (modifica/cancella/esegui) è riservato al suo **OWNER** (o
    a un admin come operatore). Un job legacy/di sistema (owner vuoto, es. il job
    di backup) è gestibile solo da un admin — così un non-owner come Giovanni non
    può cancellarlo."""
    from ..api.admin import is_admin
    principal = _caller(request)
    if is_admin(principal):
        return principal
    if principal != (job.get("owner") or ""):
        raise HTTPException(403, "solo l'owner del job (o un admin) può gestirlo")
    return principal


def _require_topic_owner(request: Request, tier: str, name: str) -> tuple[str, dict]:
    from ..api.admin import is_admin

    principal = _caller(request)
    topic = topics_client.open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "topic non trovato")
    meta = topic.get("meta", {})
    if principal != meta.get("owner") and not is_admin(principal):
        raise HTTPException(403, "solo l'owner del topic può gestire i trigger")
    return principal, meta


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/clodia/channels/{tier}/{name}/cron-trigger")
async def api_get_topic_cron_trigger(tier: str, name: str, request: Request):
    await asyncio.to_thread(_require_topic_owner, request, tier, name)
    trigger = db.get_topic_trigger(tier, name)
    if trigger and not trigger.get("interval_minutes") and trigger.get("cron_expr"):
        # Trigger LEGACY, ancora a cron: continua a girare com'è. Qui allego solo
        # la cadenza equivalente da proporre nel form — la sostituzione la decide
        # l'owner salvando, non questa GET (#239).
        trigger = dict(trigger)
        trigger["suggested_interval_minutes"] = scheduler.cron_to_interval_minutes(
            trigger["cron_expr"])
    return {"trigger": trigger}


@router.put("/clodia/channels/{tier}/{name}/cron-trigger")
async def api_put_topic_cron_trigger(
    tier: str, name: str, req: TopicCronTriggerUpsert, request: Request,
):
    owner, meta = await asyncio.to_thread(_require_topic_owner, request, tier, name)
    if req.cron_expr and req.cron_expr.strip():
        raise HTTPException(
            422, "il trigger di un topic non accetta più un'espressione cron: "
                 "usa interval_minutes + repeat_count (clodia-platform#239)")
    # Floor di frequenza per i topic trigger: ogni fire è un turno agentico, una
    # cadenza troppo alta creerebbe un backlog illimitato (issue #46/#25).
    floor_err = scheduler.validate_interval_minutes(
        req.interval_minutes,
        min_interval_minutes=scheduler.TOPIC_TRIGGER_MIN_INTERVAL_MIN)
    if floor_err:
        raise HTTPException(422, f"invalid interval_minutes: {floor_err}")
    prompt = req.prompt.strip()
    if not prompt:
        raise HTTPException(422, "prompt richiesto")
    agent = (req.agent or "").strip()
    if agent:
        _require_valid_agent(agent)
        if agent not in (meta.get("participants") or []):
            raise HTTPException(422, f"agent '{agent}' non partecipa al topic")
    existing = db.get_topic_trigger(tier, name)
    if existing:
        # Riabilitare un trigger esaurito è un RIARMO: senza azzerare il
        # contatore ripartirebbe già oltre il limite e si spegnerebbe al primo
        # fire. `update_job` azzera da sé se la cadenza cambia; qui copriamo il
        # caso «stessi numeri, ricomincia».
        riarmo = (not existing.get("enabled")
                  and existing.get("repeat_count")
                  and existing.get("fired_count", 0) >= existing["repeat_count"])
        trigger = db.update_job(
            existing["id"], prompt=prompt, agent=agent, enabled=True,
            interval_minutes=req.interval_minutes, repeat_count=req.repeat_count,
            fired_count=0 if riarmo else None,
        )
    else:
        try:
            trigger = db.create_topic_trigger(
                tier, name, prompt, interval_minutes=req.interval_minutes,
                repeat_count=req.repeat_count, agent=agent, owner=owner,
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "questo topic ha già un trigger cron")
    if trigger is None:
        raise HTTPException(404, "trigger cron non trovato")
    scheduler.register_job(trigger)
    return {"trigger": trigger}


@router.delete("/clodia/channels/{tier}/{name}/cron-trigger")
async def api_delete_topic_cron_trigger(tier: str, name: str, request: Request):
    await asyncio.to_thread(_require_topic_owner, request, tier, name)
    trigger = db.get_topic_trigger(tier, name)
    if not trigger:
        raise HTTPException(404, "trigger cron non configurato")
    scheduler.unregister_job(trigger["id"])
    db.delete_job(trigger["id"])
    return {"deleted": True}


@router.get("/clodia/jobs")
async def api_list_jobs():
    return [job for job in db.list_jobs() if job.get("mode") != "topic_trigger"]


@router.get("/clodia/jobs/{job_id}")
async def api_get_job(job_id: int):
    return _require_job(job_id)


@router.post("/clodia/jobs", status_code=201)
async def api_create_job(req: JobCreate, request: Request):
    owner = _caller(request)  # chi crea diventa owner (dev'essere autenticato)
    cron = _resolve_cron(req.cron_expr, req.schedule_text)
    _require_valid_agent(req.agent)
    try:
        job = db.create_job(
            name=req.name,
            cron_expr=cron,
            prompt=req.prompt,
            agent=req.agent,
            enabled=req.enabled,
            owner=owner,
            tier=req.tier or "",
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


@router.post("/clodia/jobs/propose", status_code=201)
async def api_propose_job(req: JobPropose):
    """Un AGENTE propone un job: NON lo crea. Registra una proposta pendente; il
    job nasce solo all'approvazione dell'owner. Gate SINCRONO: la conferma avviene
    con un popup in chat (l'owner è presente) via POST /clodia/jobs/proposals/{id}/
    decide. Sicurezza: un job è esecuzione autonoma ricorrente → deve passare
    dall'owner (Prima Legge). Il link firmato asincrono resta per i workflow."""
    from . import proposals
    from ..api import gate_sign
    cron = _resolve_cron(req.cron_expr, req.schedule_text)
    _require_valid_agent(req.agent)
    if db.get_job_by_name(req.name) is not None:
        raise HTTPException(status_code=409, detail=f"job name '{req.name}' already exists")
    prop = proposals.create(
        name=req.name, cron_expr=cron, prompt=req.prompt, agent=req.agent,
        enabled=req.enabled, requested_by=(req.requested_by or "agente"),
        nonce=gate_sign.new_nonce())
    return {
        "proposal_id": prop["id"],
        "status": "pending",
        "name": prop["name"],
        "cron_expr": cron,
        "agent": prop["agent"],
        "prompt": prop["prompt"],
        # Istruzione per l'agente: presenta il job e chiudi con questo marker, così
        # la webui mostra il popup di conferma Approva/Annulla all'owner.
        "render_marker": f"<!-- job-proposal={prop['id']} -->",
        "message": ("Proposta registrata. Presenta il job all'owner e includi il "
                    "marker `render_marker` in fondo al messaggio: comparirà un "
                    "popup Approva/Annulla. Il job nasce solo se l'owner approva."),
    }


@router.post("/clodia/jobs/proposals/{pid}/decide")
async def api_decide_proposal(pid: int, request: Request):
    """Conferma SINCRONA di una proposta (popup in chat). Autorizzata: solo un
    principal umano admin/superadmin (l'owner). Approva → crea+registra il job."""
    from . import proposals
    from ..api.admin import is_admin
    from ..api.agents import _principal_from_request
    principal = _principal_from_request(request)
    if not is_admin(principal):
        raise HTTPException(403, "solo l'owner (admin) può approvare un job")
    prop = proposals.get(pid)
    if not prop:
        raise HTTPException(404, "proposta non trovata")
    if prop.get("status") != "pending":
        raise HTTPException(409, "questa proposta non è più in attesa")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    choice = (body.get("choice") or "").strip().lower()
    try:
        return proposals.apply_decision(prop, choice, body.get("comment", ""), owner=principal)
    except proposals.ProposalError as e:
        raise HTTPException(e.status, e.detail)


@router.patch("/clodia/jobs/{job_id}")
async def api_update_job(job_id: int, req: JobUpdate, request: Request):
    _require_job_owner(request, _require_job(job_id))
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
            tier=req.tier,
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
async def api_delete_job(job_id: int, request: Request):
    _require_job_owner(request, _require_job(job_id))
    scheduler.unregister_job(job_id)
    deleted = db.delete_job(job_id)
    if not deleted:  # race
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return {"deleted": job_id}


@router.post("/clodia/jobs/{job_id}/run")
async def api_run_job(job_id: int, request: Request):
    """Fire manuale immediato (bypassa il cron). Utile per test e debug."""
    _require_job_owner(request, _require_job(job_id))
    result = await scheduler.fire_job(job_id)
    return {"job_id": job_id, **result}
