"""Channel trigger «incoming mail» (clodia-platform#628).

When the mail courier delivers mail into a room — a linked mailbox or the
room's own address (`box+CODE@`) — the room may start ONE turn of an agent of
its choosing, with a prompt written by the owner, followed by the list of the
mail just delivered. Owner's request, 28 Oct 2026.

One record per topic, a YAML file under `CLODIA_DATA/mail-triggers/`. Not a
job of the scheduler: there is no cadence to register, the courier fires it.
The same atomic write as `db._write`, for the same reason (two writers of the
same record must never leave a mixed file).

Bounded: at most `MAX_PER_HOUR` fires per room in any hour. Beyond that the
trigger does not start a turn and says so (`last_status = rate_limited`): a
mailbox that receives a burst must not turn into a burst of model calls.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from ..config import data_path

LOG = logging.getLogger("scheduler.mail_triggers")

DIR = data_path("mail-triggers")
MAX_PER_HOUR = 12
WINDOW_S = 3600
#: Mail listed in the prompt of one fire; the rest is counted.
MAX_LISTED = 20

_FIELDS = ("tier", "name", "agent", "prompt", "enabled", "owner", "fired_count",
           "fires", "last_run_at", "last_status", "created_at", "updated_at")
_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(tier: str, name: str) -> Path:
    # Topic names are slugs; the tier is SEAL-N. Neither may carry a path.
    for part in (tier, name):
        if not part or "/" in part or "\\" in part or ".." in part:
            raise ValueError(f"nome non valido per un trigger: {part!r}")
    return DIR / f"{tier}__{name}.yaml"


def _public(d: dict) -> dict:
    out = {k: d.get(k) for k in _FIELDS if k != "fires"}
    out["fires_last_hour"] = len(_recent(d.get("fires") or [], time.time()))
    return out


def _recent(fires: list, now: float) -> list[float]:
    out = []
    for f in fires or []:
        try:
            t = float(f)
        except (TypeError, ValueError):
            continue
        if now - t < WINDOW_S:
            out.append(t)
    return out


def _read(tier: str, name: str) -> Optional[dict]:
    p = _path(tier, name)
    if not p.is_file():
        return None
    try:
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        LOG.error("mail trigger %s ILLEGGIBILE: %s: %s", p.name, type(e).__name__, e)
        return None
    d["enabled"] = bool(d.get("enabled", True))
    d["fired_count"] = int(d.get("fired_count") or 0)
    d["fires"] = d.get("fires") if isinstance(d.get("fires"), list) else []
    return d


def _write(d: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    dst = _path(d["tier"], d["name"])
    tmp = dst.with_suffix(f".yaml.tmp-{os.getpid()}-{threading.get_ident()}")
    testo = yaml.safe_dump({k: d.get(k) for k in _FIELDS}, sort_keys=False,
                           allow_unicode=True)
    try:
        tmp.write_text(testo, encoding="utf-8")
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def get(tier: str, name: str) -> Optional[dict]:
    d = _read(tier, name)
    return _public(d) if d else None


def upsert(tier: str, name: str, *, agent: str, prompt: str, enabled: bool = True,
           owner: str = "") -> dict:
    with _LOCK:
        d = _read(tier, name) or {"tier": tier, "name": name, "fired_count": 0,
                                  "fires": [], "last_run_at": None,
                                  "last_status": None, "created_at": _now_iso(),
                                  "owner": owner}
        d.update(agent=agent, prompt=prompt, enabled=bool(enabled),
                 updated_at=_now_iso())
        d["owner"] = d.get("owner") or owner
        _write(d)
        return _public(d)


def delete(tier: str, name: str) -> bool:
    with _LOCK:
        p = _path(tier, name)
        if not p.is_file():
            return False
        p.unlink()
        return True


def retier(old_tier: str, new_tier: str, name: str) -> bool:
    """A topic that changes level keeps its trigger (as `retier_topic_jobs`)."""
    with _LOCK:
        d = _read(old_tier, name)
        if d is None:
            return False
        d["tier"] = new_tier
        _write(d)
        _path(old_tier, name).unlink(missing_ok=True)
        return True


def claim_fire(tier: str, name: str, now: Optional[float] = None) -> tuple[Optional[dict], str]:
    """Decide and record ONE fire: `(trigger, "")` when a turn may start,
    `(trigger_or_None, reason)` when not. Reasons: `absent`, `disabled`,
    `rate_limited`. The count and the hour window are written here, under the
    lock, so two cycles never both pass the limit."""
    now = time.time() if now is None else now
    with _LOCK:
        d = _read(tier, name)
        if d is None:
            return None, "absent"
        if not d["enabled"]:
            return _public(d), "disabled"
        recenti = _recent(d["fires"], now)
        if len(recenti) >= MAX_PER_HOUR:
            d.update(fires=recenti, last_status="rate_limited",
                     last_run_at=datetime.fromtimestamp(now, timezone.utc).isoformat())
            _write(d)
            LOG.warning("mail trigger %s/%s: %d fire nell'ultima ora, limite %d — "
                        "turno non avviato", tier, name, len(recenti), MAX_PER_HOUR)
            return _public(d), "rate_limited"
        d.update(fires=recenti + [now], fired_count=d["fired_count"] + 1,
                 last_run_at=datetime.fromtimestamp(now, timezone.utc).isoformat())
        _write(d)
        return _public(d), ""


def set_status(tier: str, name: str, status: str) -> None:
    with _LOCK:
        d = _read(tier, name)
        if d is None:
            return
        d["last_status"] = status
        _write(d)


def prompt_text(agent: str, prompt: str, items: list[dict]) -> str:
    """`@agent prompt` followed by the mail just delivered (sender, subject,
    path of the `.eml`). The prompt is the owner's text; the envelopes are
    already in the room as untrusted content."""
    righe = []
    for it in items[:MAX_LISTED]:
        mitt = str(it.get("from") or "?")[:120]
        ogg = str(it.get("subject") or "(senza oggetto)")[:160]
        path = str(it.get("eml") or "")
        righe.append(f"- {mitt} — «{ogg}»" + (f" ({path})" if path else ""))
    extra = len(items) - MAX_LISTED
    if extra > 0:
        righe.append(f"- … e altre {extra}")
    return (f"@{agent} {prompt}\n\nPosta appena consegnata in questo canale "
            f"({len(items)}):\n" + "\n".join(righe))


async def fire(tier: str, name: str, items: list[dict]) -> str:
    """Start the room's «incoming mail» turn, if it has one. Returns "" when a
    turn was started, else the reason it was not. Called by the mail courier
    with what it just delivered; the turn goes through the scheduler's door
    (a topic trigger): a system message with a synthetic principal, normal
    routing, QUEUED when the agent is busy (not skipped)."""
    import asyncio
    from ..api import channels, topics_client
    trig, motivo = await asyncio.to_thread(claim_fire, tier, name)
    if motivo:
        return motivo
    agent = str((trig or {}).get("agent") or "")
    meta = ((await topics_client.async_open_topic(tier, name)) or {}).get("meta") or {}
    if agent not in (meta.get("participants") or []):
        await asyncio.to_thread(set_status, tier, name, "agent_not_participant")
        LOG.warning("mail trigger %s/%s: '%s' non partecipa più alla stanza",
                    tier, name, agent)
        return "agent_not_participant"
    testo = prompt_text(channels.indirizzo(agent), str(trig.get("prompt") or ""), items)
    result = await channels.post_channel_message(
        tier, name, testo, "mail-trigger", kind="system", trusted_internal=True,
        skip_if_busy=False)
    started = result.get("responders") or (
        [result["responder"]] if result.get("responder") else [])
    stato = "ok" if started else "not_started"
    await asyncio.to_thread(set_status, tier, name, stato)
    return "" if started else stato
