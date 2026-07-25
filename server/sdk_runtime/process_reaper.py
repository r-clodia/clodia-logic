"""Linux process observability and last-resort cleanup for agent runtimes.

The SDK normally owns its subprocess, but a crashed/failed ``stop()`` can leave
the Claude CLI alive after the corresponding ChatSession disappeared.  This
module deliberately has a narrow kill policy: only Claude-looking descendants
of agent-server, older than the hard TTL, whose cwd is not owned by a live
session.
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


def sweep_orphan_runtime_processes(
    live_cwds: set[str], hard_ttl_seconds: float, *,
    proc_root: Path = Path("/proc"), root_pid: int | None = None,
    kill=os.kill,
) -> dict:
    """Observe Claude descendants and SIGTERM stale processes not in live cwds."""
    global _last_metrics
    rows = _read_processes(proc_root)
    descendants = _descendant_pids(rows, root_pid or os.getpid())
    live = {str(Path(path).resolve()) for path in live_cwds}
    claude = [row for row in rows if row.pid in descendants and _is_claude_command(row.command)]
    orphans = [
        row for row in claude
        if row.age_seconds >= hard_ttl_seconds and (row.cwd is None or row.cwd not in live)
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
        "reaped_total": _last_metrics["reaped_total"] + reaped,
        "updated_at": time.time(),
    }
    return {**_last_metrics, "reaped": reaped,
            "processes": [asdict(row) for row in claude]}
