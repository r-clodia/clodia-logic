"""Processes: the deterministic side of the colony (clodia-platform#738).

Two kinds of things run here:

- a **spawn** is a process that uses an LLM — agent turns, agentic jobs. The
  activity log (`agents/activity_log.py`) and the leaderboard count them, in
  tokens;
- a **process** is deterministic: no model, no tokens. The mail courier and the
  topic indexer are asyncio loops in this server; the backup and every other
  `mode: logic` job run from the scheduler. They have runs, a duration, an
  outcome and an error — and until #738 the courier had none of these visible:
  when it stopped on 15–17 Nov nothing in the UI said so.

The in-process loops register here and wrap each pass in `run()`. Each pass
is one line in `agent-state/processes/<name>/YYYY-MM-DD.jsonl`, kept apart from
`activity/` on purpose: that directory is read as "one folder per agent" by the
leaderboard, and a process is not an agent. Logic jobs keep their history in
the scheduler (`scheduler/db.py`), and `list_processes()` reads it from there
rather than writing it twice.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .config import data_path

LOG = logging.getLogger("agent-server.processes")

PROCESS_DIR = data_path("agent-state") / "processes"

#: Same windows as the per-topic usage on the Activity page (#732).
WINDOWS = {"24h": 86400, "7d": 7 * 86400, "4w": 28 * 86400}

#: Process states shown in the UI. `failed` is the outcome of the LAST pass:
#: one good pass clears it, so a transient error does not paint the row red
#: forever, and a loop that keeps failing stays red.
STATES = ("running", "idle", "failed", "disabled")


@dataclass
class Process:
    name: str
    label: str
    interval_s: int
    enabled: bool = True
    status: str = "idle"
    last_started_at: Optional[str] = None
    last_finished_at: Optional[str] = None
    last_duration_s: Optional[float] = None
    last_ok: Optional[bool] = None
    last_error: Optional[str] = None
    last_counters: dict = field(default_factory=dict)
    next_run_at: Optional[str] = None
    _wake: Optional[asyncio.Event] = field(default=None, repr=False)

    def public(self) -> dict:
        return {
            "kind": "process",
            "source": "loop",
            "name": self.name,
            "label": self.label,
            "interval_s": self.interval_s,
            "enabled": self.enabled,
            "status": "disabled" if not self.enabled else self.status,
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "last_duration_s": self.last_duration_s,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "last_counters": dict(self.last_counters),
            "next_run_at": self.next_run_at if self.enabled else None,
            "can_run_now": self.enabled,
        }


_REGISTRY: dict[str, Process] = {}


def _iso(t: Optional[float] = None) -> str:
    return datetime.fromtimestamp(t if t is not None else time.time(),
                                  timezone.utc).isoformat()


def register(name: str, label: str, interval_s: int, *, enabled: bool = True) -> Process:
    """Declare a process. Called by the lifespan for every loop, also the ones
    switched off by configuration: a process that is off must say so in the UI,
    not disappear from it."""
    p = _REGISTRY.get(name)
    if p is None:
        p = Process(name=name, label=label, interval_s=int(interval_s), enabled=enabled)
        _REGISTRY[name] = p
    else:
        p.label, p.interval_s, p.enabled = label, int(interval_s), enabled
    return p


def get(name: str) -> Optional[Process]:
    return _REGISTRY.get(name)


def _file_for(name: str, when: Optional[datetime] = None) -> Path:
    when = when or datetime.now(timezone.utc)
    return PROCESS_DIR / name / f"{when.strftime('%Y-%m-%d')}.jsonl"


def _append(name: str, entry: dict) -> None:
    path = _file_for(name)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        LOG.error("process log write failed (%s): %s", name, e)


def _counters_of(result) -> dict:
    """What a pass returns, as counters. The loops return an int today (mail
    delivered, topics indexed); a dict is taken as it is, so #735/#736 can add
    `fetched`/`sent`/`failed` without touching this module."""
    if isinstance(result, dict):
        return {str(k): v for k, v in result.items() if isinstance(v, (int, float))}
    if isinstance(result, bool) or result is None:
        return {}
    if isinstance(result, (int, float)):
        return {"items": result}
    return {}


async def run(name: str, fn: Callable[[], Awaitable]) -> object:
    """Run one pass of process `name`, record it, and return what `fn` returned.

    Never raises: the loops call this inside `while True`, and an exception that
    escaped here would end the loop — the failure that #738 exists to make
    visible would then make the process invisible again."""
    p = _REGISTRY.get(name) or register(name, name, 0)
    t0 = time.time()
    p.status = "running"
    p.last_started_at = _iso(t0)
    result, error = None, None
    try:
        result = await fn()
    except asyncio.CancelledError:
        p.status = "idle"
        raise
    except Exception as e:  # noqa: BLE001 — recorded, then the loop goes on
        # The type only: an exception message from a mail pass can quote an
        # address or a subject, and this file is read by the admin UI.
        error = type(e).__name__
        LOG.warning("process %s: %s", name, error)
    t1 = time.time()
    counters = _counters_of(result)
    if error is None and counters.get("errors"):
        # A pass that went on past a failing part (a transport of the
        # courier, #737) still failed at something: say so, by count.
        error = f"{int(counters['errors'])} errori nel giro"
    p.last_finished_at = _iso(t1)
    p.last_duration_s = round(t1 - t0, 3)
    p.last_ok = error is None
    p.last_error = error
    p.last_counters = counters
    p.status = "idle" if error is None else "failed"
    p.next_run_at = _iso(t1 + p.interval_s) if p.interval_s else None
    _append(name, {"ts": p.last_started_at, "finished_at": p.last_finished_at,
                   "duration_s": p.last_duration_s, "ok": p.last_ok,
                   "error": error, "counters": counters})
    try:
        from .core.events import bus
        from .core.models import Event
        await bus.publish(Event(type="process_run", payload={"process": name, **p.public()},
                                timestamp=datetime.now(timezone.utc)))
    except Exception:  # noqa: BLE001 — the live stream is best effort
        pass
    return result


async def sleep(name: str, seconds: float) -> bool:
    """Sleep until the next pass, or less if someone asked for one now.
    Returns True when woken by `request_run`."""
    p = _REGISTRY.get(name)
    if p is None:
        await asyncio.sleep(seconds)
        return False
    if p._wake is None:
        p._wake = asyncio.Event()
    p.next_run_at = _iso(time.time() + seconds)
    try:
        await asyncio.wait_for(p._wake.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        p._wake.clear()


def request_run(name: str) -> bool:
    """Ask a loop process for a pass now. False if it does not exist, is off,
    or is already in a pass (the request would only queue a second one)."""
    p = _REGISTRY.get(name)
    if p is None or not p.enabled:
        return False
    if p._wake is None:
        p._wake = asyncio.Event()
    p._wake.set()
    return True


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _empty_windows() -> dict:
    return {w: {"runs": 0, "failures": 0, "seconds": 0.0} for w in WINDOWS}


def _ts(raw) -> Optional[float]:
    try:
        t = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.timestamp()


def _add(win: dict, ts: Optional[float], ok: bool, seconds, now: float) -> None:
    if ts is None:
        return
    age = now - ts
    for w, span in WINDOWS.items():
        if 0 <= age <= span:
            win[w]["runs"] += 1
            win[w]["failures"] += 0 if ok else 1
            win[w]["seconds"] += float(seconds or 0)


def loop_windows(name: str, now: Optional[float] = None) -> dict:
    """Runs, failures and seconds of a loop process over 24h / 7d / 4w, read
    from the last 29 daily files only (4 weeks plus the day boundary)."""
    now = now if now is not None else time.time()
    win = _empty_windows()
    pdir = PROCESS_DIR / name
    if not pdir.is_dir():
        return win
    for f in sorted(pdir.glob("*.jsonl"))[-29:]:
        for e in _read_jsonl(f):
            _add(win, _ts(e.get("ts")), bool(e.get("ok")), e.get("duration_s"), now)
    for w in win.values():
        w["seconds"] = round(w["seconds"], 1)
    return win


def loop_history(name: str, limit: int = 50) -> list[dict]:
    """The last `limit` passes of a loop process, newest first."""
    pdir = PROCESS_DIR / name
    if not pdir.is_dir():
        return []
    out: list[dict] = []
    for f in reversed(sorted(pdir.glob("*.jsonl"))[-29:]):
        out.extend(reversed(_read_jsonl(f)))
        if len(out) >= limit:
            break
    return out[:limit]


#: Run states of a job (`scheduler/db.py`) that count as a failure here.
_JOB_FAILED = ("error", "fatal", "failed", "missed")


def job_windows(job: dict, now: Optional[float] = None) -> dict:
    now = now if now is not None else time.time()
    win = _empty_windows()
    for r in job.get("runs") or []:
        stato = str(r.get("stato") or "")
        if stato == "running":
            continue
        _add(win, _ts(r.get("ts")), stato not in _JOB_FAILED, r.get("durata"), now)
    for w in win.values():
        w["seconds"] = round(w["seconds"], 1)
    return win


def _job_status(job: dict) -> str:
    if not job.get("enabled", True):
        return "disabled"
    runs = job.get("runs") or []
    last = runs[-1] if runs else None
    if last is None:
        return "idle"
    stato = str(last.get("stato") or "")
    if stato == "running":
        return "running"
    return "failed" if stato in _JOB_FAILED else "idle"


def job_process(job: dict, next_run_at: Optional[str] = None) -> dict:
    """A `mode: logic` job seen as a process. The job stays a job — edited,
    enabled and run from the Jobs page; this is its read-only face."""
    runs = job.get("runs") or []
    last = next((r for r in reversed(runs) if r.get("stato") != "running"), None)
    return {
        "kind": "process",
        "source": "job",
        "name": f"job:{job.get('id')}",
        "job_id": job.get("id"),
        "label": job.get("name") or f"job {job.get('id')}",
        "cron": job.get("cron_expr"),
        "enabled": bool(job.get("enabled", True)),
        "status": _job_status(job),
        "last_started_at": (last or {}).get("ts"),
        "last_finished_at": (last or {}).get("finished_at"),
        "last_duration_s": (last or {}).get("durata"),
        "last_ok": None if last is None else str(last.get("stato")) not in _JOB_FAILED,
        "last_error": (last or {}).get("error"),
        "last_counters": {},
        "next_run_at": next_run_at,
        "can_run_now": False,     # the Jobs page runs it, with its own checks
    }


def list_processes(jobs: list[dict], next_fire: Callable[[int], Optional[str]] = lambda _i: None,
                   now: Optional[float] = None) -> list[dict]:
    """Every process: the loops registered here, then the logic jobs."""
    out = []
    for p in sorted(_REGISTRY.values(), key=lambda x: x.label.lower()):
        out.append({**p.public(), "windows": loop_windows(p.name, now)})
    for job in sorted((j for j in jobs if j.get("mode") == "logic"),
                      key=lambda j: str(j.get("name") or "").lower()):
        try:
            nxt = next_fire(int(job.get("id")))
        except Exception:  # noqa: BLE001
            nxt = None
        out.append({**job_process(job, nxt), "windows": job_windows(job, now)})
    return out
