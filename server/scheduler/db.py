"""Persistenza dei job dello scheduler — file-per-job (clodia-data/jobs/<id>.yaml).

Sostituisce il vecchio SQLite (agent-state/jobs.db): i job sono ora file YAML
**editabili** e **clonabili** (un clone nuovo parte con jobs/ vuoto). Stessa
interfaccia del modulo precedente, così api.py e scheduler.py restano invariati.

Gerarchia seed → job → spawn: il job è la definizione durevole di lavoro
schedulato; quando parte materializza uno spawn dell'executor.

Schema job (jobs/<id>.yaml):
    id, name, cron_expr, prompt, agent, enabled, last_run_at, last_status,
    last_chat_id, created_at, updated_at

`agent` = nome dell'agent (kind) che lo scheduler spawna al fire del job;
risolto dinamicamente (statico clodia/ada/looper/ophelia o seed del registry).
Job creati prima dell'introduzione del campo (19 giu 2026) → default "looper"
in lettura, per preservarne il comportamento storico.
"""
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
    "last_run_at", "last_status", "last_chat_id", "created_at", "updated_at",
)

# Agent di fallback per job senza il campo `agent` (creati prima del 19 giu 2026).
_LEGACY_DEFAULT_AGENT = "looper"


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
               owner: str = "") -> dict:
    """Crea un nuovo job. Solleva sqlite3.IntegrityError se 'name' è duplicato
    (contratto invariato con api.py → HTTP 409). `owner` = principal umano che ne
    è proprietario (solo lui, o un admin, può agirvi)."""
    if get_job_by_name(name) is not None:
        raise sqlite3.IntegrityError(f"job name '{name}' already exists")
    now = _now_iso()
    d = {
        "id": _next_id(), "name": name, "cron_expr": cron_expr, "prompt": prompt,
        "agent": agent or "clodia", "owner": owner or "",
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


def update_job(
    job_id: int,
    *,
    name: Optional[str] = None,
    cron_expr: Optional[str] = None,
    prompt: Optional[str] = None,
    agent: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> Optional[dict]:
    """Aggiorna i campi non None. Ritorna il job aggiornato o None se non esiste."""
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


def mark_run(job_id: int, *, status: str, chat_id: Optional[str] = None) -> None:
    """Aggiorna last_run_at / last_status / last_chat_id dopo un fire."""
    d = get_job(job_id)
    if d is None:
        return
    d["last_run_at"] = _now_iso()
    d["last_status"] = status
    d["last_chat_id"] = chat_id
    d["updated_at"] = d["last_run_at"]
    _write(d)


def iter_enabled_jobs() -> Iterable[dict]:
    """Itera sui job enabled (per il bootstrap dello scheduler)."""
    for j in _all():
        if j.get("enabled"):
            yield j
