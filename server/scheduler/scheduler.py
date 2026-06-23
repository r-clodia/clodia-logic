"""APScheduler integration: spawn di chat Looper effimere al fire dei cron.

Architettura:
- BackgroundScheduler con jobstore in-memory; la fonte di verità dei job è
  file-per-job (jobs/<id>.yaml, vedi db.py), da cui lo schedule è ricostruito
  al boot e on-change.
- Timezone fissa Europe/Rome (l'utente pensa in locale).
- Il modulo conserva una reference al loop asyncio di FastAPI per fare
  bridging dal thread APScheduler (`run_coroutine_threadsafe`) verso le
  coroutine del ChatManager.
- `fire_job` crea una chat kind='looper', le impone il titolo `[CRON] <name>`
  e le invia il prompt in fire-and-forget. Persiste last_run_at, last_status,
  last_chat_id sul DB.
"""
import asyncio
import logging
from datetime import datetime, timezone
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

def validate_cron_expr(expr: str) -> Optional[str]:
    """Valida un'espressione cron a 5 campi.

    Ritorna None se valida, stringa con motivo dell'errore altrimenti.
    Granularità minima: 1 minuto (lo accetta CronTrigger nativamente).
    """
    if not isinstance(expr, str) or not expr.strip():
        return "cron_expr empty"
    try:
        CronTrigger.from_crontab(expr.strip(), timezone=_SCHED_TZ)
        return None
    except (ValueError, TypeError) as e:
        return str(e)


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
    _scheduler.add_job(
        _fire_job_threadsafe,
        trigger=trigger,
        id=_job_key(job["id"]),
        replace_existing=True,
        coalesce=True,
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

    agent = job.get("agent") or "clodia"
    if not known_kind(agent):
        LOG.warning("fire_job: job %s agent '%s' ignoto → fallback clodia", job_id, agent)
        agent = "clodia"

    LOG.info("Firing job id=%s name=%s agent=%s", job_id, job["name"], agent)
    chat_id: Optional[str] = None
    try:
        chat = await manager.create(kind=agent)
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
        db.mark_run(job_id, status="ok", chat_id=chat_id)
        return {"chat_id": chat_id, "status": "ok"}
    except Exception as e:
        LOG.exception("Errore firing job %s: %s", job_id, e)
        db.mark_run(job_id, status=f"error: {e}", chat_id=chat_id)
        return {"chat_id": chat_id, "status": f"error: {e}"}
