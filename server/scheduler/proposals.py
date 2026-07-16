"""Proposte di job in attesa di approvazione dell'owner.

Un agente NON crea job direttamente (un job è una concessione permanente di
esecuzione autonoma ricorrente → superficie di privilegio, Prima Legge): propone
un job, l'owner lo approva via link firmato one-time (gate). Solo all'approvazione
la proposta diventa un job reale (`db.create_job` + `scheduler.register_job`).

Storage: file YAML sotto `CLODIA_DATA/job_proposals/<id>.yaml`, stesso pattern
persistente dei job (`db.py`). Nessuna dipendenza nuova.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from ..config import data_path

PROPOSALS_DIR = data_path("job_proposals")

_FIELDS = (
    "id", "name", "cron_expr", "prompt", "agent", "enabled",
    "requested_by", "nonce", "status", "comment", "created_at", "resolved_at",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(pid: int) -> Path:
    return PROPOSALS_DIR / f"{pid}.yaml"


def _read(p: Path) -> Optional[dict]:
    try:
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    return d if "id" in d else None


def _write(d: dict) -> None:
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {k: d.get(k) for k in _FIELDS}
    _path(d["id"]).write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _all() -> list[dict]:
    if not PROPOSALS_DIR.is_dir():
        return []
    out = [_read(p) for p in sorted(PROPOSALS_DIR.glob("*.yaml"))]
    return sorted([d for d in out if d], key=lambda x: x["id"])


def _next_id() -> int:
    ids = [d["id"] for d in _all() if isinstance(d.get("id"), int)]
    return (max(ids) + 1) if ids else 1


def create(name: str, cron_expr: str, prompt: str, agent: str, enabled: bool,
           requested_by: str, nonce: str) -> dict:
    """Registra una proposta pendente."""
    d = {
        "id": _next_id(), "name": name, "cron_expr": cron_expr, "prompt": prompt,
        "agent": agent, "enabled": bool(enabled),
        "requested_by": requested_by, "nonce": nonce,
        "status": "pending", "comment": "",
        "created_at": _now_iso(), "resolved_at": None,
    }
    _write(d)
    return d


def get(pid: int) -> Optional[dict]:
    return _read(_path(pid))


def resolve(pid: int, status: str, comment: str = "") -> Optional[dict]:
    """Segna la proposta come approved|rejected e consuma il nonce (one-time)."""
    d = get(pid)
    if d is None:
        return None
    d["status"] = status
    d["comment"] = comment or ""
    d["nonce"] = None  # one-time: il link muore
    d["resolved_at"] = _now_iso()
    _write(d)
    return d


def list_pending() -> list[dict]:
    return [d for d in _all() if d.get("status") == "pending"]


class ProposalError(Exception):
    """Errore applicativo con status HTTP suggerito (tradotto dal layer web)."""
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def apply_decision(prop: dict, choice: str, comment: str = "") -> dict:
    """Applica la decisione dell'owner su una proposta (approva → crea+registra
    il job; annulla → rifiuta). Condivisa dal gate SINCRONO (popup in chat) e da
    quello ASINCRONO (link firmato)."""
    from . import db, scheduler
    import sqlite3
    if choice.startswith("approva"):
        resolve(prop["id"], "approved", comment)
        try:
            job = db.create_job(
                name=prop["name"], cron_expr=prop["cron_expr"],
                prompt=prop["prompt"], agent=prop["agent"],
                enabled=bool(prop.get("enabled", True)))
        except sqlite3.IntegrityError:
            raise ProposalError(409, f"esiste già un job con nome '{prop['name']}'")
        if job.get("enabled"):
            try:
                scheduler.register_job(job)
            except Exception as e:  # noqa: BLE001
                raise ProposalError(500, f"job creato (id={job['id']}) ma "
                                         f"registrazione scheduler fallita: {e}")
        return {"ok": True, "outcome": "approvato", "job_id": job["id"]}
    resolve(prop["id"], "rejected", comment)
    return {"ok": True, "outcome": "annullato"}
