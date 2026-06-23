"""Activity log per agente.

Persiste eventi in JSONL (un file/giorno/agente) sotto
`CLODIA_DATA/agent-state/activity/<agent>/YYYY-MM-DD.jsonl` e li ripubblica
sull'event bus globale per lo streaming live (SSE).

Eventi correnti:
- run_started    { from, inbox, depth }
- run_done       { duration_s, report }
- run_error      { duration_s, error }
- handoff_attach { file }
- handoff_comment { length }
- handoff_fork   { to_agent, sub_card_id }
- handoff_move   { to_inbox, sender }
- handoff_archive { lane }

Future estensioni (D in roadmap): tool_use, message_chunk, thinking_chunk.
"""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import data_path
from ..core.events import bus
from ..core.models import Event

LOG = logging.getLogger("agent-server.agents.activity")

ACTIVITY_DIR = data_path("agent-state") / "activity"


def _file_for(agent: str, when: Optional[datetime] = None) -> Path:
    when = when or datetime.now(timezone.utc)
    return ACTIVITY_DIR / agent / f"{when.strftime('%Y-%m-%d')}.jsonl"


def append(
    agent: str,
    event_type: str,
    payload: dict,
    task_id: Optional[str] = None,
    card_id: Optional[str] = None,
) -> None:
    """Append sincrono. Pubblica anche sul bus se siamo in un event loop."""
    now = datetime.now(timezone.utc)
    entry = {
        "ts": now.isoformat(),
        "agent": agent,
        "type": event_type,
        "task_id": task_id,
        "card_id": card_id,
        "payload": payload or {},
    }
    path = _file_for(agent, now)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        LOG.error("Activity log write failed (%s/%s): %s", agent, event_type, e)
    # Publish best-effort (no errore se non c'è event loop)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(bus.publish(Event(
                type=f"agent_activity",
                payload={**entry},
                timestamp=now,
            )))
    except RuntimeError:
        pass


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _agent_log_files(agent: str) -> list[Path]:
    """File di log dell'agente, ordinati per data crescente (il nome è YYYY-MM-DD)."""
    adir = ACTIVITY_DIR / agent
    if not adir.is_dir():
        return []
    return sorted(adir.glob("*.jsonl"))


def tail(agent: str, limit: int = 200, date: Optional[str] = None) -> list[dict]:
    """Ultimi `limit` eventi dell'agente. Se `date` è dato → solo quel giorno;
    altrimenti AGGREGA gli ultimi giorni disponibili (così la tab Logs non si
    svuota al cambio data o dopo un riavvio quando non c'è ancora attività oggi)."""
    if date:
        try:
            when = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
        except ValueError:
            when = datetime.now(timezone.utc)
        return _read_jsonl(_file_for(agent, when))[-limit:]
    out: list[dict] = []
    # dal più recente all'indietro finché non raggiungo `limit`
    for f in reversed(_agent_log_files(agent)[-30:]):
        out = _read_jsonl(f) + out
        if len(out) >= limit:
            break
    return out[-limit:]


def summary() -> list[dict]:
    """Per ogni agente con almeno un file di log: { agent, today_runs,
    last_run, status }."""
    out = []
    if not ACTIVITY_DIR.is_dir():
        return out
    for child in sorted(ACTIVITY_DIR.iterdir()):
        if not child.is_dir():
            continue
        agent = child.name
        runs = 0
        status = "idle"
        last_run_ts = None
        last_event = None
        # today_runs = run di oggi; status/last_event dall'ultimo file con eventi
        # (così non si azzerano al cambio data / dopo un riavvio).
        for e in _read_jsonl(_file_for(agent)):
            if e.get("type") == "run_started":
                runs += 1
        files = _agent_log_files(agent)
        for f in reversed(files):
            evs = _read_jsonl(f)
            if not evs:
                continue
            for e in evs:
                if e.get("type") == "run_started":
                    status = "running"
                    last_run_ts = e.get("ts")
                elif e.get("type") in ("run_done", "run_error"):
                    status = "idle" if e.get("type") == "run_done" else "error"
            last_event = evs[-1]
            break
        out.append({
            "agent": agent,
            "today_runs": runs,
            "status": status,
            "last_run_ts": last_run_ts,
            "last_event_ts": last_event.get("ts") if last_event else None,
            "last_event_type": last_event.get("type") if last_event else None,
        })
    return out
