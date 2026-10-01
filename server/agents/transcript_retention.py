"""Agent transcripts under the retention of their tier (clodia-platform#446, part 2).

The transcripts (`agent-state/transcripts/<agent>/<chat key>/*.jsonl`) hold
the full content of the turns of a room — for a SEAL-2 topic, customer data —
outside the topic store, and nothing removed them. The chat key carries the
tier (`chan_SEAL-2_<topic>_<agent>`), so each file is judged by the clock of
its tier:

    CLODIA_TRANSCRIPT_RETENTION = "SEAL-0=365,SEAL-1=365,SEAL-2=365,SEAL-3=365,SEAL-4=365"

days per tier, `0` = keep; default 365 for every tier. A transcript whose key
carries no tier (a direct chat, a job) falls under `CLODIA_TRANSCRIPT_RETENTION_OTHER`
(default 365). Every run is a `control.retention` event on the trail with
what was removed, per tier.

The same loop prunes the stored turn reasoning (`reasoning_log`, #484), whose
retention per tier is min(this policy, `reasoning_log.MAX_DAYS`), with an event
of its own (`resource: "reasoning"`).
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .transcripts import TRANSCRIPTS_DIR

_TIERS = ("SEAL-0", "SEAL-1", "SEAL-2", "SEAL-3", "SEAL-4")
DEFAULT_DAYS = 365
_TIER_IN_KEY = re.compile(r"(?:^|_)(SEAL-[0-4])_")


def policy() -> dict[str, int]:
    out = {t: DEFAULT_DAYS for t in _TIERS}
    out["other"] = DEFAULT_DAYS
    raw = (os.environ.get("CLODIA_TRANSCRIPT_RETENTION") or "").strip()
    for part in raw.split(","):
        if "=" in part:
            k, v = (x.strip() for x in part.split("=", 1))
            if k.upper() in out:
                try:
                    out[k.upper()] = max(0, int(v))
                except ValueError:
                    pass
    try:
        out["other"] = max(0, int(os.environ.get("CLODIA_TRANSCRIPT_RETENTION_OTHER")
                                  or DEFAULT_DAYS))
    except ValueError:
        pass
    return out


def tier_of(chat_key: str) -> str:
    m = _TIER_IN_KEY.search(chat_key)
    return m.group(1) if m else "other"


def apply(root: Path | None = None, now: float | None = None) -> dict[str, int]:
    root = root or TRANSCRIPTS_DIR
    now = now or time.time()
    pol = policy()
    removed: dict[str, int] = {}
    if not root.is_dir():
        return removed
    for f in root.rglob("*.jsonl"):
        tier = tier_of(f.parent.name)
        days = pol.get(tier, DEFAULT_DAYS)
        if days and f.stat().st_mtime < now - days * 86400:
            f.unlink()
            removed[tier] = removed.get(tier, 0) + 1
    return removed


async def run_once() -> None:
    """One retention pass: transcripts, then the stored turn reasoning (#484).

    Each store gets its own `control.retention` event, so the trail says what
    was removed (`resource`), per tier (`result.removed`), under which policy.
    A failure in one store does not skip the other.
    """
    import asyncio
    import logging
    from .. import audit_events
    from . import reasoning_log
    log = logging.getLogger("agent-server.transcripts")
    for resource, prune, pol in (("transcripts", apply, policy),
                                 ("reasoning", reasoning_log.purge, reasoning_log.policy)):
        try:
            removed = await asyncio.to_thread(prune)
            await audit_events.report({
                "type": "control.retention", "action": "apply", "resource": resource,
                "actor": {"type": "service", "id": "agent-server"},
                "result": {"removed": removed or None, "policy": pol()}})
        except Exception as e:  # noqa: BLE001 - the loop must survive
            log.error("%s retention failed: %s", resource, e)


async def retention_loop(interval: int = 86400) -> None:
    import asyncio
    while True:
        await run_once()
        await asyncio.sleep(interval)
