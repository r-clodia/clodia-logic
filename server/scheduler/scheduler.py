"""APScheduler integration per job agentici, logici e trigger dei topic.

Architettura:
- BackgroundScheduler con jobstore in-memory; la fonte di verità dei job è
  file-per-job (jobs/<id>.yaml, vedi db.py), da cui lo schedule è ricostruito
  al boot e on-change.
- Timezone fissa Europe/Rome (l'utente pensa in locale).
- Il modulo conserva una reference al loop asyncio di FastAPI per fare
  bridging dal thread APScheduler (`run_coroutine_threadsafe`) verso le
  coroutine del ChatManager.
- `fire_job` esegue un piano logico, crea una chat agentica oppure posta il
  prompt nel topic associato, secondo il mode del record.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

try:
    # zoneinfo è stdlib su Python ≥ 3.9
    from zoneinfo import ZoneInfo
    _SCHED_TZ = ZoneInfo("Europe/Rome")
except Exception:  # pragma: no cover
    import pytz
    _SCHED_TZ = pytz.timezone("Europe/Rome")

from . import db
from ..core.events import bus
from ..core.models import Event
from ..sdk_runtime.session import known_kind, manager

LOG = logging.getLogger("scheduler")

# State globale del modulo. Usiamo singleton perché lo scheduler è una
# risorsa unica per processo (FastAPI app è singleton).
_scheduler: Optional[BackgroundScheduler] = None
_loop: Optional[asyncio.AbstractEventLoop] = None


# ---------------------------------------------------------------------------
# Validazione cron
# ---------------------------------------------------------------------------

# Intervallo minimo fra due fire per i TOPIC TRIGGER (issue #46): ogni fire è un
# turno agentico → una frequenza troppo alta creerebbe un backlog illimitato di
# turni (stesso vettore del job test */5 che saturò agent-server, #25). Ai job
# globali NON si applica (possono avere ragioni legittime per essere più densi).
TOPIC_TRIGGER_MIN_INTERVAL_MIN = 10


def min_cron_gap_minutes(expr: str, samples: int = 30) -> float:
    """Minimo intervallo in minuti fra fire consecutivi di `expr`. `inf` se
    produce meno di 2 fire nell'orizzonte campionato. Cattura `*/1`, `* * * * *`,
    `0,5 * * * *`, ecc. — non solo il campo minuti."""
    trig = CronTrigger.from_crontab(expr.strip(), timezone=_SCHED_TZ)
    now = datetime.now(_SCHED_TZ)
    times: list[datetime] = []
    prev = None
    for _ in range(samples):
        nxt = trig.get_next_fire_time(prev, now if prev is None else prev + timedelta(seconds=1))
        if nxt is None:
            break
        times.append(nxt)
        prev = nxt
    if len(times) < 2:
        return float("inf")
    return min((times[i + 1] - times[i]).total_seconds() / 60.0
               for i in range(len(times) - 1))


def validate_cron_expr(expr: str, *, min_interval_minutes: int = 0) -> Optional[str]:
    """Valida un'espressione cron a 5 campi.

    Ritorna None se valida, stringa con motivo dell'errore altrimenti.
    Con `min_interval_minutes > 0` rifiuta anche cron che firano più spesso di
    quel floor (per i topic trigger: floor = TOPIC_TRIGGER_MIN_INTERVAL_MIN).
    """
    if not isinstance(expr, str) or not expr.strip():
        return "cron_expr empty"
    try:
        CronTrigger.from_crontab(expr.strip(), timezone=_SCHED_TZ)
    except (ValueError, TypeError) as e:
        return str(e)
    if min_interval_minutes > 0:
        gap = min_cron_gap_minutes(expr)
        if gap < min_interval_minutes:
            return (f"intervallo minimo {min_interval_minutes} min: questo cron "
                    f"fira ogni ~{gap:.0f} min")
    return None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def _job_key(job_id: int) -> str:
    """Chiave stabile per APScheduler (così possiamo replace/remove by id)."""
    return f"clodia-job-{job_id}"


def start_scheduler(loop: asyncio.AbstractEventLoop) -> BackgroundScheduler:
    """Avvia lo scheduler. Idempotente: se già avviato ritorna l'istanza
    esistente. Salva una reference al loop FastAPI per bridging async."""
    global _scheduler, _loop
    if _scheduler is not None:
        return _scheduler
    _loop = loop
    # Jobstore in-memory: la fonte di verità sono i file jobs/<id>.yaml (db.py),
    # da cui sync_jobs_from_db ricostruisce lo schedule al boot e on-change.
    _scheduler = BackgroundScheduler(timezone=_SCHED_TZ)
    _scheduler.start()
    LOG.info("Scheduler avviato (timezone=Europe/Rome, jobs da %s)", db.JOBS_DIR)
    return _scheduler


def shutdown_scheduler() -> None:
    """Stop dello scheduler senza attendere il completamento dei job in volo."""
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
        LOG.info("Scheduler fermato")
    except Exception as e:
        LOG.warning("Errore in scheduler.shutdown: %s", e)
    _scheduler = None


# ---------------------------------------------------------------------------
# Register / unregister / reload
# ---------------------------------------------------------------------------

def register_job(job: dict) -> None:
    """Registra (o sostituisce) un job in APScheduler a partire dal record DB.

    Idempotente grazie a replace_existing=True.
    """
    if _scheduler is None:
        raise RuntimeError("scheduler not started")
    trigger = CronTrigger.from_crontab(job["cron_expr"], timezone=_SCHED_TZ)
    # Topic trigger: max_instances=1 → APScheduler non avvia un fire se il
    # precedente è ancora in esecuzione (belt; lo skip-if-busy vero è sul turno
    # del responder in _fire_topic_trigger).
    max_instances = 1 if job.get("mode") == "topic_trigger" else 3
    _scheduler.add_job(
        _fire_job_threadsafe,
        trigger=trigger,
        id=_job_key(job["id"]),
        replace_existing=True,
        coalesce=True,
        max_instances=max_instances,
        misfire_grace_time=60,
        args=[job["id"]],
        name=job["name"],
    )
    LOG.info(
        "Registered job id=%s name=%s cron='%s'",
        job["id"], job["name"], job["cron_expr"],
    )


def unregister_job(job_id: int) -> bool:
    """Rimuove un job da APScheduler. Ritorna True se rimosso, False
    se non c'era."""
    if _scheduler is None:
        return False
    try:
        _scheduler.remove_job(_job_key(job_id))
        LOG.info("Unregistered job id=%s", job_id)
        return True
    except Exception:
        return False


def reload_all_enabled_jobs() -> int:
    """Riconcilia APScheduler con lo stato del DB.

    Strategia: clean slate. Rimuoviamo tutti i job preesistenti nel jobstore
    (potrebbero essere reliquati di job cancellati dal DB mentre il server era
    spento) e ri-registriamo solo i job `enabled=1`.

    Ritorna il numero di job registrati.
    """
    if _scheduler is None:
        raise RuntimeError("scheduler not started")
    try:
        _scheduler.remove_all_jobs()
    except Exception as e:  # pragma: no cover
        LOG.warning("remove_all_jobs failed (proseguo): %s", e)
    n = 0
    for job in db.iter_enabled_jobs():
        try:
            register_job(job)
            n += 1
        except Exception as e:
            LOG.error("Errore registrando job id=%s: %s", job.get("id"), e)
    LOG.info("Reloaded %d enabled jobs from db", n)
    return n


# ---------------------------------------------------------------------------
# Fire: spawn della chat Looper
# ---------------------------------------------------------------------------

def _fire_job_threadsafe(job_id: int) -> None:
    """Callback eseguito dal thread di APScheduler.

    APScheduler con BackgroundScheduler esegue i job in un thread pool. Le
    operazioni del ChatManager sono coroutine sul loop FastAPI: usiamo
    `run_coroutine_threadsafe` per dispacciare il fire sul loop corretto.
    """
    if _loop is None:
        LOG.error("No FastAPI loop reference; cannot fire job %s", job_id)
        return
    if _loop.is_closed():
        LOG.error("FastAPI loop is closed; cannot fire job %s", job_id)
        return
    asyncio.run_coroutine_threadsafe(fire_job(job_id), _loop)


def _call_logic_run(verb: str, args: dict) -> dict:
    """Esegue UN verbo di un job logico via l'endpoint interno del gateway
    (orchestrator-secret, allowlist, NO gate). Sincrono → chiamare in un thread."""
    import os
    import requests
    base = (os.environ.get("CLODIA_TOOLS_URL")
            or os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/")
            .replace("/mcp/", "").rstrip("/"))
    secret = (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip()
    r = requests.post(f"{base.rstrip('/')}/internal/logic-run",
                      json={"verb": verb, "args": args or {}},
                      headers={"X-Orchestrator-Secret": secret}, timeout=900)
    r.raise_for_status()
    return r.json()


async def _fire_logic_job(job: dict) -> dict:
    """Esegue il PIANO di un job logico (lista di {verb, args}) step-by-step, senza
    LLM. Aggiorna last_run. Fermarsi al primo step fallito."""
    plan = job.get("plan") or []
    LOG.info("Firing job LOGICO id=%s name=%s (%d step)", job["id"], job["name"], len(plan))
    steps: list[dict] = []
    try:
        if not plan:
            raise RuntimeError("job logico senza piano (plan vuoto)")
        for st in plan:
            verb = (st or {}).get("verb")
            args = (st or {}).get("args") or {}
            if not verb:
                raise RuntimeError("step senza 'verb'")
            res = await asyncio.to_thread(_call_logic_run, verb, args)
            steps.append({"verb": verb, "ok": res.get("ok")})
            if not res.get("ok"):
                raise RuntimeError(f"step '{verb}' fallito: {res.get('error')}")
        db.mark_run(job["id"], status="ok", chat_id=None)
        LOG.info("job LOGICO id=%s ok (%s)", job["id"], steps)
        return {"chat_id": None, "status": "ok", "steps": steps}
    except Exception as e:  # noqa: BLE001
        LOG.error("job LOGICO id=%s fallito: %s", job["id"], e)
        db.mark_run(job["id"], status=f"error: {e}", chat_id=None)
        return {"chat_id": None, "status": f"error: {e}", "steps": steps}


async def _fire_topic_trigger(job: dict) -> dict:
    """Post the configured prompt into its topic and use channel routing."""
    from ..api import channels

    tier = str(job.get("topic_tier") or "")
    name = str(job.get("topic_name") or "")
    if not tier or not name:
        raise RuntimeError("topic trigger senza tier/name")
    prompt = str(job.get("prompt") or "").strip()
    agent = str(job.get("agent") or "").strip()
    # SKIP-IF-BUSY (issue #46): se il turno precedente del responder è ancora in
    # corso, NON triggerare il successivo. Per un agente esplicito lo verifichiamo
    # PRIMA di postare (salta l'intero fire, niente prompt-stale accumulato); per
    # il caso routed lo demandiamo a post_channel_message (skip_if_busy).
    if agent and channels._responder_busy(tier, name, agent):
        status = "skipped (turno precedente ancora in corso)"
        db.mark_run(job["id"], status=status, chat_id=f"topic:{tier}/{name}")
        return {"chat_id": f"topic:{tier}/{name}", "status": "skipped",
                "topic": f"{tier}/{name}", "skipped": [agent]}
    content = f"@{agent} {prompt}" if agent else prompt
    result = await channels.post_channel_message(
        tier,
        name,
        content,
        "scheduler",
        kind="system",
        trusted_internal=True,
        skip_if_busy=True,
    )
    started = result.get("responders") or (
        [result["responder"]] if result.get("responder") else [])
    if not started and result.get("skipped"):
        status = "skipped (turno precedente ancora in corso)"
        outcome = "skipped"
    else:
        status = "dispatched (messaggio postato nel topic)"
        outcome = "dispatched"
    db.mark_run(job["id"], status=status, chat_id=f"topic:{tier}/{name}")
    return {
        "chat_id": f"topic:{tier}/{name}",
        "status": outcome,
        "topic": f"{tier}/{name}",
        "responder": result.get("responder"),
        "responders": result.get("responders"),
        "skipped": result.get("skipped") or [],
    }


async def fire_job(job_id: int) -> dict:
    """Spawna una chat effimera dell'agent indicato dal job e le consegna il
    prompt in fire-and-forget.

    L'agent (`job['agent']`) è risolto dinamicamente: kind statico
    (clodia/ada/looper/ophelia) o un agent del registry (seed). Se l'agent non
    è (più) noto, fallback a "clodia" con warning — un job non deve fallire
    silenziosamente per una definizione di agent diventata stale.

    Aggiorna last_run_at / last_status / last_chat_id sul DB.

    Ritorna `{'chat_id': str|None, 'status': 'ok'|'error: ...'}` — utile per
    l'endpoint manuale `POST /clodia/jobs/{id}/run`.
    """
    job = db.get_job(job_id)
    if job is None:
        LOG.warning("fire_job: job %s non trovato (forse appena cancellato)", job_id)
        return {"chat_id": None, "status": "error: job not found"}

    # JOB LOGICO: piano deterministico di verbi, nessun turno LLM né gate
    # (pre-autorizzato alla creazione). Esegue via l'endpoint interno del gateway.
    if (job.get("mode") or "agentic") == "logic":
        return await _fire_logic_job(job)
    if job.get("mode") == "topic_trigger":
        try:
            return await _fire_topic_trigger(job)
        except Exception as e:  # noqa: BLE001
            LOG.exception("Errore firing topic trigger %s: %s", job_id, e)
            db.mark_run(job_id, status=f"error: {e}", chat_id=None)
            return {"chat_id": None, "status": f"error: {e}"}

    agent = job.get("agent") or "clodia"
    if not known_kind(agent):
        LOG.warning("fire_job: job %s agent '%s' ignoto → fallback clodia", job_id, agent)
        agent = "clodia"

    LOG.info("Firing job id=%s name=%s agent=%s", job_id, job["name"], agent)
    chat_id: Optional[str] = None
    try:
        chat = await manager.create(kind=agent, run_id=f"job:{job_id}")
        chat_id = chat.chat_id
        # Titolo custom: lo spec richiede "[CRON] <job-name>". Va impostato
        # PRIMA di consegnare il prompt — `_record()` sovrascrive il titolo
        # solo se è uno dei valori di default ("", "Nuova chat", "[LOOP] ...").
        # Una stringa "[CRON] ..." non è in quel set, quindi resta intatta.
        chat.title = f"[CRON] {job['name']}"
        # `manager.create()` ha già pubblicato `chat_created` col titolo di default
        # — emettiamo `chat_updated` così la UI (SSE) riflette subito il rename.
        await bus.publish(Event(
            type="chat_updated",
            payload=chat.to_dict(),
            timestamp=datetime.now(timezone.utc),
        ))
        # Fire-and-forget: il turno parte in background sulla chat.
        # Lo scheduler non blocca aspettando la risposta del Looper.
        await chat.send_user_message_async(job["prompt"])
        # fire-and-forget: il turno prosegue async → NON è un "ok" reale, è un
        # 'dispatched' (l'esito del turno non è tracciato qui). Evita falsi successi.
        db.mark_run(job_id, status="dispatched (turno avviato in background)", chat_id=chat_id)
        return {"chat_id": chat_id, "status": "dispatched"}
    except Exception as e:
        LOG.exception("Errore firing job %s: %s", job_id, e)
        db.mark_run(job_id, status=f"error: {e}", chat_id=chat_id)
        return {"chat_id": chat_id, "status": f"error: {e}"}
