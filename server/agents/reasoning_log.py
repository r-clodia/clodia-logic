"""Stored REASONING of a turn, per channel (clodia-platform#484).

`thinking_chunk` used to be an event and nothing more: published on the bus
while streaming and consumed only by the clients connected at that moment.
Whoever reopened the topic after the turn had ended — the normal case, since a
chat is read after the fact — had no way to see what the agent had reasoned.
It was not a collapsed box to expand: the data no longer existed.

**Why a store of its own and not the activity log**, which already exists and
persists events per agent: the activity log is indexed by AGENT and *does not
know which tier the event happened in* (`agent-state/activity/<agent>/
YYYY-MM-DD.jsonl`). The reasoning of a SEAL-4 turn and that of a SEAL-0 turn
would end up in the same file, and reasoning **quotes the channel's content**.
Here the first directory of the path is the tier and the second the channel:
containment lives in the structure, before the guard of the route that serves
it (`_require_member` in `api/channels.py`).

Format: `agent-state/reasoning/<tier>/<channel>/YYYY-MM-DD.jsonl`, one line per
BUBBLE — the key is the id of the message that appeared in the channel, because
the bubble is what one looks at when asking "how did it get there".

Permissions: directories are created 0o700 and files 0o600. Sandboxed agent
uids rely on file permissions, not on this process, to keep them out.

Retention: per tier, the shorter of the tier's transcript retention
(`transcript_retention.policy()`, i.e. `CLODIA_TRANSCRIPT_RETENTION`, #446) and
`MAX_DAYS`. A tier configured to keep transcripts forever (`0`) still drops
reasoning after `MAX_DAYS`. Pruning runs in `transcript_retention.retention_loop`
and is reported on the audit trail like the transcript pruning.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..config import data_path

LOG = logging.getLogger("agent-server.agents.reasoning")

REASONING_DIR = data_path("agent-state") / "reasoning"
#: Tool actions kept per turn, and characters of each action's summary (#484).
MAX_TOOLS = 200
TOOL_SUMMARY_CHARS = 200

#: Per-turn cap: 32k head + 32k tail (owner's decision, #484). BOTH ends are
#: kept, not just one: the tail is the conclusion — the part one comes back to
#: read — and the head is how the problem was framed, without which the
#: conclusion makes no sense. What is missing is the middle, and the text says
#: so instead of hiding it.
HEAD_CHARS = 32 * 1024
TAIL_CHARS = 32 * 1024

#: Upper bound of the retention, in days, for every tier. Reasoning is the
#: bulkiest data the platform writes per turn: without an expiry the store
#: grows forever. A tier whose transcript retention is shorter uses that one.
MAX_DAYS = 90

DIR_MODE = 0o700
FILE_MODE = 0o600

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _channel_dir(tier: str, name: str) -> Path:
    """The channel's directory, rejecting anything that could escape it.

    `tier` and `name` come from the path of an HTTP route: validation lives
    here, in the only place that turns them into a path, not in every caller.
    """
    for part in (tier, name):
        if not _SEGMENT.match(part or ""):
            raise ValueError(f"path segment not allowed: {part!r}")
    return REASONING_DIR / tier / name


def _ensure_private_dir(tier: str, name: str) -> Path:
    """Create the channel directory (and the root/tier levels) with mode 0o700."""
    target = _channel_dir(tier, name)
    REASONING_DIR.parent.mkdir(parents=True, exist_ok=True)
    for d in (REASONING_DIR, REASONING_DIR / tier, target):
        d.mkdir(mode=DIR_MODE, exist_ok=True)
    return target


def cap(text: str) -> tuple[str, bool]:
    """The text within the cap, and whether it was cut. See `HEAD_CHARS`/`TAIL_CHARS`."""
    if len(text) <= HEAD_CHARS + TAIL_CHARS:
        return text, False
    omitted = len(text) - HEAD_CHARS - TAIL_CHARS
    return (f"{text[:HEAD_CHARS]}\n\n[… {omitted} characters of reasoning omitted …]\n\n"
            f"{text[-TAIL_CHARS:]}"), True


def _today_file(tier: str, name: str, when: Optional[datetime] = None) -> Path:
    when = when or datetime.now(timezone.utc)
    return _channel_dir(tier, name) / f"{when.strftime('%Y-%m-%d')}.jsonl"


def record(tier: str, name: str, *, message_id: str, spawn: str, seed: str,
           text: str, chat_id: str | None = None,
           tools: list | None = None, tools_omitted: int = 0) -> None:
    """Attach to a bubble the reasoning of the turn that produced it: its
    thinking text and/or its tool actions, i.e. what the live box showed."""
    tools = [{"tool": str(t.get("tool") or "tool")[:80],
              "input_summary": str(t.get("input_summary") or "")[:TOOL_SUMMARY_CHARS]}
             for t in (tools or [])[:MAX_TOOLS] if isinstance(t, dict)]
    if not (text or "").strip() and not tools:
        return
    body, truncated = cap(text or "")
    path = _today_file(tier, name)
    _ensure_private_dir(tier, name)
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "message_id": message_id,
        "spawn": spawn,
        "seed": seed,
        "chat_id": chat_id,
        "chars": len(body),
        "truncated": truncated,
        "text": body,
        "tools": tools,
        "tools_omitted": int(tools_omitted or 0),
    }
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def _daily_files(tier: str, name: str) -> list[Path]:
    try:
        folder = _channel_dir(tier, name)
    except ValueError:
        return []
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.jsonl") if _DAY.match(p.stem))


def _lines(path: Path):
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            # A truncated line (interrupted write) must not make the rest of
            # the day unreadable.
            continue


def index(tier: str, name: str, days: int = 30) -> list[str]:
    """The ids of the messages that HAVE stored reasoning.

    This is what lights the 💭 on a bubble in the UI: a button that opens
    nothing is worse than no button, so the list is the store's truth and not
    a guess by the client ("it is an agent's message, so it must have thought").
    """
    out: list[str] = []
    seen: set[str] = set()
    for f in _daily_files(tier, name)[-days:]:
        for line in _lines(f):
            mid = line.get("message_id")
            if mid and mid not in seen:
                seen.add(mid)
                out.append(mid)
    return out


def read(tier: str, name: str, message_id: str) -> dict | None:
    """The reasoning of ONE bubble, or None. The last write wins."""
    found = None
    for f in _daily_files(tier, name):
        for line in _lines(f):
            if line.get("message_id") == message_id:
                found = line
    return found


def policy() -> dict[str, int]:
    """Retention in days per tier: min(tier transcript retention, MAX_DAYS).

    A transcript retention of `0` means "keep forever"; reasoning never does,
    so it falls back to `MAX_DAYS`. Keys are the transcript policy's keys
    (`SEAL-0` … `SEAL-4`, `other`).
    """
    from . import transcript_retention
    return {tier: (min(days, MAX_DAYS) if days > 0 else MAX_DAYS)
            for tier, days in transcript_retention.policy().items()}


def purge(now: Optional[datetime] = None) -> dict[str, int]:
    """Remove the daily files past their tier's retention. Returns {tier: count}.

    Only files that can be dated are removed: a file whose name is not a date
    is left alone, because a cleanup that guesses is a way of losing data. A
    tier directory that is not a known tier falls under `other`.
    """
    removed: dict[str, int] = {}
    if not REASONING_DIR.is_dir():
        return removed
    now = now or datetime.now(timezone.utc)
    pol = policy()
    for f in REASONING_DIR.glob("*/*/*.jsonl"):
        tier = f.parent.parent.name
        key = tier if tier in pol else "other"
        days = pol.get(key, MAX_DAYS)
        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        if not _DAY.match(f.stem) or f.stem >= cutoff:
            continue
        try:
            f.unlink()
            removed[key] = removed.get(key, 0) + 1
        except OSError as e:  # noqa: PERF203 — best effort, one file at a time
            LOG.warning("reasoning retention: %s not removed (%s)", f, e)
    return removed
