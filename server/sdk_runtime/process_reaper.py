"""Linux process observability and last-resort cleanup for agent runtimes.

The SDK normally owns its subprocess, but a crashed/failed ``stop()`` can leave
the Claude CLI alive after the corresponding ChatSession disappeared.  This
module deliberately has a narrow kill policy: only Claude-looking descendants
of agent-server, older than the hard TTL, whose pid is not claimed by a live
session (nor a descendant of one), whose cwd is readable, and whose cwd is not
owned by a live session.

Why the pid is the primary guard (clodia-platform#478).  Until 1 Oct 2026 the
only ownership proof was the *cwd*: a process whose resolved cwd was missing
from ``live_cwds`` was an orphan by definition.  That inference is indirect —
it holds only as long as the path kept in memory keeps resolving to the same
string as ``/proc/<pid>/cwd`` — and when it broke, the reaper sent SIGTERM to
the CLI of a turn that was running (exit 143 mid-query, no answer in chat).
A live session knows the pid of its own subprocess: that is the direct fact,
and an inference must never outrank it.  The cwd rule survives only as the
second net, for processes nobody claims.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import signal
import time


@dataclass(frozen=True)
class RuntimeProcess:
    pid: int
    ppid: int
    age_seconds: float
    rss_bytes: int
    cwd: str | None
    command: str


_last_metrics = {
    "live_processes": 0,
    "rss_bytes": 0,
    "orphan_processes": 0,
    "protected_processes": 0,
    "reaped_total": 0,
    "updated_at": None,
}


def runtime_process_metrics() -> dict:
    return dict(_last_metrics)


def _read_processes(proc_root: Path = Path("/proc")) -> list[RuntimeProcess]:
    """Read a process snapshot. Returns an empty list on non-Linux platforms."""
    try:
        uptime = float((proc_root / "uptime").read_text().split()[0])
        clock_ticks = os.sysconf("SC_CLK_TCK")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return []
    rows: list[RuntimeProcess] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # comm can contain spaces and parentheses: fields start after ") ".
            stat_tail = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
            ppid = int(stat_tail[1])
            started = int(stat_tail[19]) / clock_ticks
            resident = int((entry / "statm").read_text().split()[1]) * page_size
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            ).strip()
            try:
                cwd = str((entry / "cwd").resolve(strict=True))
            except OSError:
                cwd = None
            rows.append(RuntimeProcess(
                pid=int(entry.name), ppid=ppid,
                age_seconds=max(0.0, uptime - started),
                rss_bytes=resident, cwd=cwd, command=command,
            ))
        except (OSError, ValueError, IndexError):
            continue  # process exited, or is not inspectable
    return rows


def _is_claude_command(command: str) -> bool:
    parts = command.lower().split()
    return any(
        Path(part).name in {"claude", "claude.exe"}
        or "claude-code" in part
        or "@anthropic-ai/claude-code" in part
        for part in parts
    )


def _descendant_pids(rows: list[RuntimeProcess], root_pid: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for row in rows:
        children.setdefault(row.ppid, []).append(row.pid)
    found: set[int] = set()
    pending = list(children.get(root_pid, ()))
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        found.add(pid)
        pending.extend(children.get(pid, ()))
    return found


def _protected_pids(rows: list[RuntimeProcess], claimed: set[int]) -> set[int]:
    """The claimed pids plus everything they spawned.

    A live CLI can fork helpers of its own (and those look like Claude too):
    killing a child of a running turn is the same incident as killing its
    parent, so protection is inherited downwards.
    """
    protected = {pid for pid in claimed if isinstance(pid, int)}
    for pid in list(protected):
        protected |= _descendant_pids(rows, pid)
    return protected


def sweep_orphan_runtime_processes(
    live_cwds: set[str], hard_ttl_seconds: float, *,
    protected_pids: set[int] | None = None,
    proc_root: Path = Path("/proc"), root_pid: int | None = None,
    kill=os.kill,
) -> dict:
    """Observe Claude descendants and SIGTERM stale, unclaimed processes.

    ``protected_pids`` are the subprocesses live sessions declare as their own
    (``ChatManager.live_runtime_pids``). They are never a target, at any age and
    whatever their cwd resolves to.
    """
    global _last_metrics
    rows = _read_processes(proc_root)
    descendants = _descendant_pids(rows, root_pid or os.getpid())
    live = {str(Path(path).resolve()) for path in live_cwds}
    protected = _protected_pids(rows, set(protected_pids or ()))
    claude = [row for row in rows if row.pid in descendants and _is_claude_command(row.command)]
    orphans = [
        row for row in claude
        # cwd unreadable = unknown owner, and an unknown owner is not a proven
        # orphan: deliberately we let it live until the next restart rather than
        # risk a second #478 (a live turn killed on a path we failed to read).
        if row.age_seconds >= hard_ttl_seconds
        and row.pid not in protected
        and row.cwd is not None and row.cwd not in live
    ]
    reaped = 0
    # Children first, so a parent cannot immediately respawn them while exiting.
    for row in reversed(orphans):
        try:
            kill(row.pid, signal.SIGTERM)
            reaped += 1
        except (ProcessLookupError, PermissionError):
            continue
    _last_metrics = {
        "live_processes": len(claude),
        "rss_bytes": sum(row.rss_bytes for row in claude),
        "orphan_processes": len(orphans),
        "protected_processes": sum(1 for row in claude if row.pid in protected),
        "reaped_total": _last_metrics["reaped_total"] + reaped,
        "updated_at": time.time(),
    }
    # `orphans` and `live_cwds` travel with the stats so the tick can log the
    # two sets side by side: #478 could not be diagnosed from the logs because
    # the kill decision printed its verdict and none of its inputs.
    return {**_last_metrics, "reaped": reaped,
            "processes": [asdict(row) for row in claude],
            "orphans": [asdict(row) for row in orphans],
            "live_cwds": sorted(live)}
