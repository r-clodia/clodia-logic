"""Workflows dichiarativi (pack ops) — store dei run.

I pack dichiarano `workflows:` nel manifest del plugin (composizione di
skill in stage/lane); qui vive lo STATO delle esecuzioni: un run = una card
che attraversa le lane. File-per-run in `CLODIA_DATA/workflows/runs/` —
dentro la datadir, quindi nel perimetro di backup senza fare nulla.

Lane = SKILL richiesta, non agente (decisione giugno 2026): l'assegnazione
del worker avviene per capability a ogni stage, nel motore.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..config import data_path

RUNS_DIR_NAME = "workflows/runs"

RUN_STATUSES = ("pending", "running", "await", "done",
                "failed", "cancelled")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}$")


def _runs_dir() -> Path:
    d = data_path(RUNS_DIR_NAME)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_TERMINAL = ("done", "failed", "cancelled")


def _seq_file() -> Path:
    # Fuori da runs/ così il glob "*.json" di list_runs non lo raccoglie.
    d = data_path("workflows")
    d.mkdir(parents=True, exist_ok=True)
    return d / "run_seq.json"


def _next_seq(plugin: str, workflow: str) -> int:
    """Contatore monotòno per workflow (sopravvive alla cancellazione dei run)."""
    key = f"{plugin}/{workflow}"
    p = _seq_file()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except Exception:  # noqa: BLE001
        data = {}
    n = int(data.get(key, 0)) + 1
    data[key] = n
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return n


# ── definizioni (dai manifest dei plugin) ────────────────────────────────────
def available_workflows() -> dict[str, dict]:
    """{"<plugin>/<workflow>": {plugin, name, trigger, stages}} dai plugin installati."""
    out: dict[str, dict] = {}
    for manifest in sorted(Path(data_path("plugins")).glob("*/plugin.yaml")):
        try:
            meta = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(meta, dict):
            continue
        plugin = manifest.parent.name
        for wname, wf in (meta.get("workflows") or {}).items():
            if isinstance(wf, dict) and wf.get("stages"):
                out[f"{plugin}/{wname}"] = {
                    "plugin": plugin, "name": wname,
                    "trigger": wf.get("trigger") or ["api"],
                    "tier": wf.get("tier") or "SEAL-1",
                    "owner": wf.get("owner") or "",
                    "workspace": wf.get("workspace"),
                    "stages": wf["stages"],
                }
    return out


# ── run CRUD ─────────────────────────────────────────────────────────────────
def create_run(plugin: str, workflow: str, *, title: str, params: str = "",
               topic: dict | None = None, requested_by: str = "") -> dict:
    defs = available_workflows()
    key = f"{plugin}/{workflow}"
    if key not in defs:
        raise KeyError(f"workflow sconosciuto: {key}")
    if not _NAME_RE.fullmatch(plugin) or not _NAME_RE.fullmatch(workflow):
        raise ValueError("nomi plugin/workflow non validi")
    seq = _next_seq(plugin, workflow)
    auto_name = f"{workflow} #{seq}"
    run = {
        "id": f"{workflow}-{secrets.token_hex(4)}",
        "seq": seq,                      # progressivo per workflow
        "name": auto_name,               # nome leggibile: "{workflow} #N"
        "plugin": plugin,
        "workflow": workflow,
        "title": title or auto_name,     # etichetta mostrata (default = nome auto)
        "params": params,
        "topic": topic or None,          # {tier, name} opzionale: la pratica di riferimento
        "requested_by": requested_by,
        "tier": defs[key].get("tier", "SEAL-1"),   # tier del topic effimero
        "wf_owner": defs[key].get("owner", ""),     # agente umano responsabile (notifiche)
        "workspace_cfg": defs[key].get("workspace"), # {repo, dir, credential} o None
        "workspace_path": None,                      # path del clone (popolato all'avvio)
        "gate_nonce": None,                          # one-time token del gate corrente
        # snapshot delle stage alla creazione: un run non cambia se il pack
        # viene aggiornato a metà corsa.
        "stages": defs[key]["stages"],
        "current": 0,                    # indice stage corrente
        "status": "pending",
        "gate_pending": False,           # await su un gate (vs intake)?
        "await_marker": None,            # # messaggi nel topic all'ingresso in await
        "history": [],                   # [{lane, skill, agent, started_at, finished_at, status, summary}]
        "approvals": [],                 # [{stage, by, verdict, note, at}]
        "created_at": _now(),
        "started_at": _now(),            # datetime di inizio del run
        "ended_at": None,                # datetime di fine (stampata al terminale)
        "updated_at": _now(),
    }
    save_run(run)
    return run


def run_path(run_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9_-]+-[0-9a-f]{8}", run_id):
        raise ValueError("run id non valido")
    return _runs_dir() / f"{run_id}.json"


def load_run(run_id: str) -> dict | None:
    p = run_path(run_id)
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def delete_run(run_id: str) -> bool:
    """Rimuove definitivamente il file del run. True se c'era, False altrimenti.
    Non tocca il contatore seq (monotòno: i numeri non si riusano)."""
    p = run_path(run_id)
    if not p.is_file():
        return False
    p.unlink()
    return True


def save_run(run: dict) -> None:
    run["updated_at"] = _now()
    # Stampa la datetime di fine alla PRIMA transizione a stato terminale,
    # qualunque sia il percorso (done/failed/cancelled). Punto unico.
    if run.get("status") in _TERMINAL and not run.get("ended_at"):
        run["ended_at"] = _now()
    p = run_path(run["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def list_runs(include_done: bool = True) -> list[dict]:
    out = []
    for p in sorted(_runs_dir().glob("*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not include_done and r.get("status") in ("done", "failed", "cancelled"):
            continue
        out.append(r)
    out.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return out
