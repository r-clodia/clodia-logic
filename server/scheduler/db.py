"""Persistenza dei job dello scheduler — file-per-job (clodia-data/jobs/<id>.yaml).

Sostituisce il vecchio SQLite (agent-state/jobs.db): i job sono ora file YAML
**editabili** e **clonabili** (un clone nuovo parte con jobs/ vuoto). Stessa
interfaccia del modulo precedente, così api.py e scheduler.py restano invariati.

Gerarchia seed → job → spawn: il job è la definizione durevole di lavoro
schedulato; quando parte materializza uno spawn dell'executor.

Schema job (jobs/<id>.yaml):
    id, name, cron_expr, prompt, agent, enabled, tier, last_run_at, last_status,
    last_chat_id, topic_tier, topic_name, created_at, updated_at

`agent` = nome dell'agent (kind) che lo scheduler spawna al fire del job;
risolto dinamicamente (statico clodia/ada/looper/ophelia o seed del registry).
Job creati prima dell'introduzione del campo (19 giu 2026) → default "looper"
in lettura, per preservarne il comportamento storico.

**UN** nome, mai una lista (R11 del router notebook, clodia-platform#213): uno
scope asincrono ha un solo responder, ed è su quella certezza che il router si
permette di non girare mai su un job. In scrittura una lista è RIFIUTATA
(`create_job`/`update_job` → ValueError); in lettura è coercizzata al primo nome
con un warning, perché questi file si editano a mano e far sparire da
`list_jobs()` un job programmato sarebbe un guasto peggiore di quello curato.
"""
import logging
import sqlite3  # solo per IntegrityError: contratto con api.py sul nome duplicato
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import yaml

from ..config import data_path

# Directory persistente dei job sotto CLODIA_DATA (volume montato).
JOBS_DIR = data_path("jobs")

_FIELDS = (
    "id", "name", "cron_expr", "prompt", "agent", "enabled", "owner",
    "mode", "plan", "tier", "topic_tier", "topic_name", "runs", "run_seq",
    "last_run_at", "last_status", "last_chat_id", "created_at", "updated_at",
)

# Agent di fallback per job senza il campo `agent` (creati prima del 19 giu 2026).
_LEGACY_DEFAULT_AGENT = "looper"

LOG = logging.getLogger("scheduler.db")


def _one_agent_name(agent):
    """Guardia di scrittura per R11: `agent` è UN nome, o niente.

    `None` passa (= «non mi pronuncio»: default in `create_job`, campo non
    toccato in `update_job`) e `""` resta lecito (job `logic`/`topic_trigger`,
    che non hanno responder). Tutto il resto — lista, tupla, dict — è rifiutato
    qui, un solo punto per entrambe le porte di scrittura, invece di una guardia
    per chiamante: `create_topic_trigger` e `proposals.approve` la ereditano.
    """
    if agent is None or isinstance(agent, str):
        return agent
    raise ValueError(
        f"R11: un job ha UN agente, non {type(agent).__name__} ({agent!r}). "
        "Uno scope asincrono con due responder non ha una regola su chi "
        "risponde: se il campo va allargato, va deciso — non subito "
        "(clodia-platform#213).")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(job_id: int) -> Path:
    return JOBS_DIR / f"{job_id}.yaml"


def init_db() -> None:
    """Crea la directory jobs/ se non esiste (no-op se già presente)."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)


def _read(p: Path) -> Optional[dict]:
    try:
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if "id" not in d:
        return None
    d["enabled"] = bool(d.get("enabled", True))
    d["mode"] = d.get("mode") or "agentic"
    # Lettura tollerante, scrittura severa: il file può essere stato scritto a
    # mano (vedi intestazione), quindi qui la lista non la rifiuto — il job
    # sparirebbe da list_jobs() e non partirebbe più, che è peggio. Prendo il
    # primo nome e lo dico nel log: in scrittura c'è una persona che legge
    # l'errore, qui no.
    if isinstance(d.get("agent"), (list, tuple)):
        _lista = list(d["agent"])
        d["agent"] = str(_lista[0]) if _lista else ""
        LOG.warning(
            "job %s (%s): campo `agent` con %d nomi %r → uso il primo (%r). "
            "R11: un job ha UN agente (clodia-platform#213).",
            d.get("id"), d.get("name"), len(_lista), _lista, d["agent"])
    # Default legacy agent SOLO per i job agentici: un job LOGICO non ha agent
    # (nessun turno LLM) → resta vuoto, non coercizzato a 'looper'.
    if d["mode"] in ("logic", "topic_trigger"):
        d["agent"] = d.get("agent") or ""
    else:
        d["agent"] = d.get("agent") or _LEGACY_DEFAULT_AGENT
    # Job legacy (pre-owner) → owner vuoto = di sistema: solo un admin può agirvi.
    d["owner"] = d.get("owner") or ""
    return d


def _write(d: dict) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {k: d.get(k) for k in _FIELDS}
    _path(d["id"]).write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _all() -> list[dict]:
    if not JOBS_DIR.is_dir():
        return []
    out = []
    for p in sorted(JOBS_DIR.glob("*.yaml")):
        d = _read(p)
        if d is not None:
            out.append(d)
    return sorted(out, key=lambda j: j["id"])


def _next_id() -> int:
    ids = [j["id"] for j in _all() if isinstance(j.get("id"), int)]
    return (max(ids) + 1) if ids else 1


def create_job(name: str, cron_expr: str, prompt: str,
               agent: str = "clodia", enabled: bool = True,
               owner: str = "", mode: str = "agentic",
               plan: list | None = None, tier: str = "",
               topic_tier: str = "", topic_name: str = "") -> dict:
    """Crea un nuovo job. Solleva sqlite3.IntegrityError se 'name' è duplicato
    (contratto invariato con api.py → HTTP 409). `owner` = principal umano che ne
    è proprietario (solo lui, o un admin, può agirvi).

    `mode`: 'agentic' (default, turno LLM sul `prompt`) o 'logic' (esegue `plan`,
    lista di {verb, args}, senza LLM né gate — pre-autorizzato dalla creazione).

    Solleva ValueError se `agent` non è un nome solo (R11, clodia-platform#213)."""
    agent = _one_agent_name(agent)   # R11: un job ha UN agente (#213)
    if get_job_by_name(name) is not None:
        raise sqlite3.IntegrityError(f"job name '{name}' already exists")
    now = _now_iso()
    _mode = mode or "agentic"
    d = {
        "id": _next_id(), "name": name, "cron_expr": cron_expr, "prompt": prompt,
        # Job logico/topic → agent opzionale o assente; agentico → agent o clodia.
        "agent": (agent or "") if _mode in ("logic", "topic_trigger")
                 else (agent or "clodia"),
        "owner": owner or "",
        "mode": _mode, "plan": plan or [],
        # Tier RICHIESTO dal job: il livello dei dati che tratterà. Assente = non
        # dichiarato = nessun requisito, che è il comportamento di ogni job
        # esistente. Un default diverso da "" farebbe fallire al primo fire job
        # che oggi girano.
        "tier": tier or "",
        "topic_tier": topic_tier or "", "topic_name": topic_name or "",
        "enabled": bool(enabled), "last_run_at": None, "last_status": None,
        "last_chat_id": None, "created_at": now, "updated_at": now,
    }
    _write(d)
    return d


def get_job(job_id: int) -> Optional[dict]:
    p = _path(job_id)
    return _read(p) if p.is_file() else None


def get_job_by_name(name: str) -> Optional[dict]:
    for j in _all():
        if j.get("name") == name:
            return j
    return None


def list_jobs() -> list[dict]:
    return _all()


def get_topic_trigger(tier: str, name: str) -> Optional[dict]:
    """Return the single cron trigger attached to a topic, if configured."""
    return next((
        job for job in _all()
        if job.get("mode") == "topic_trigger"
        and job.get("topic_tier") == tier
        and job.get("topic_name") == name
    ), None)


def create_topic_trigger(
    tier: str,
    name: str,
    cron_expr: str,
    prompt: str,
    *,
    agent: str = "",
    owner: str = "",
) -> dict:
    if get_topic_trigger(tier, name) is not None:
        raise sqlite3.IntegrityError(f"topic trigger '{tier}/{name}' already exists")
    return create_job(
        name=f"topic-trigger:{tier}/{name}"[:200],
        cron_expr=cron_expr,
        prompt=prompt,
        agent=agent,
        owner=owner,
        mode="topic_trigger",
        topic_tier=tier,
        topic_name=name,
    )


def update_job(
    job_id: int,
    *,
    name: Optional[str] = None,
    cron_expr: Optional[str] = None,
    prompt: Optional[str] = None,
    agent: Optional[str] = None,
    enabled: Optional[bool] = None,
    tier: Optional[str] = None,
) -> Optional[dict]:
    """Aggiorna i campi non None. Ritorna il job aggiornato o None se non esiste."""
    agent = _one_agent_name(agent)   # R11: prima di toccare il file (#213)
    d = get_job(job_id)
    if d is None:
        return None
    if name is not None:
        other = get_job_by_name(name)
        if other is not None and other["id"] != job_id:
            raise sqlite3.IntegrityError(f"job name '{name}' already exists")
        d["name"] = name
    if cron_expr is not None:
        d["cron_expr"] = cron_expr
    if prompt is not None:
        d["prompt"] = prompt
    if agent is not None:
        d["agent"] = agent
    if enabled is not None:
        d["enabled"] = bool(enabled)
    if tier is not None:
        # `""` TOGLIE il requisito, `None` non si pronuncia. Senza la
        # distinzione un tier dichiarato per errore non si potrebbe più
        # rimuovere se non riscrivendo il file a mano.
        d["tier"] = tier
    d["updated_at"] = _now_iso()
    _write(d)
    return d


def delete_job(job_id: int) -> bool:
    """Ritorna True se ha cancellato qualcosa, False se non esisteva."""
    p = _path(job_id)
    if p.is_file():
        p.unlink()
        return True
    return False


_RUNS_CAP = 100  # storico run tenuto per job (i più recenti)


def mark_run(job_id: int, *, status: str, chat_id: Optional[str] = None) -> Optional[str]:
    """Registra un fire nello STORICO run del job (`runs`) e aggiorna i campi
    `last_*`. Ritorna l'id del run, usato dal completamento asincrono per
    aggiornare la stessa entry. Lo storico è cappato agli ultimi _RUNS_CAP."""
    d = get_job(job_id)
    if d is None:
        return None
    ts = _now_iso()
    s = str(status)
    if s.startswith(("error", "failed")):
        stato = "failed"
    elif s.startswith("dispatched"):
        stato = "running"
    elif s.startswith(("ok", "success", "completed")):
        stato = "success"
    else:
        stato = s.split(" ", 1)[0]
    seq = int(d.get("run_seq") or 0) + 1
    entry = {
        "id": str(seq),
        "ts": ts,
        "stato": stato,
        "chat_id": chat_id,
        "error": s.split(":", 1)[-1].strip() or s if stato == "failed" else None,
        "note": s if stato == "running" else None,
    }
    runs = list(d.get("runs") or [])
    runs.append(entry)
    d["run_seq"] = seq
    d["runs"] = runs[-_RUNS_CAP:]
    d["last_run_at"] = ts
    d["last_status"] = status
    d["last_chat_id"] = chat_id
    d["updated_at"] = ts
    _write(d)
    return entry["id"]


def complete_run(job_id: int, run_id: str, *, success: bool,
                 error: Optional[str] = None) -> bool:
    """Porta uno specifico run agentico a stato terminale e ne persiste durata.

    L'id evita che il completamento tardivo di un run aggiorni per errore una
    esecuzione successiva dello stesso job.
    """
    d = get_job(job_id)
    if d is None:
        return False
    runs = list(d.get("runs") or [])
    entry = next((row for row in runs if str(row.get("id")) == str(run_id)), None)
    if entry is None:
        return False
    finished_at = _now_iso()
    try:
        started = datetime.fromisoformat(str(entry.get("ts")))
        finished = datetime.fromisoformat(finished_at)
        duration = max(0.0, (finished - started).total_seconds())
    except (TypeError, ValueError):
        duration = None
    entry.update({
        "stato": "success" if success else "failed",
        "finished_at": finished_at,
        "durata": duration,
        "error": None if success else (str(error or "errore sconosciuto")),
        "note": None,
    })
    d["runs"] = runs
    if str(d.get("run_seq")) == str(run_id):
        d["last_status"] = "success" if success else f"failed: {error or 'errore sconosciuto'}"
    d["updated_at"] = finished_at
    _write(d)
    return True


def iter_enabled_jobs() -> Iterable[dict]:
    """Itera sui job enabled (per il bootstrap dello scheduler)."""
    for j in _all():
        if j.get("enabled"):
            yield j
