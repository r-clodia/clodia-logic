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

REASONING is not stored here, and that is not an oversight (clodia-platform#484).
This file is indexed by AGENT and **does not know which tier** it is writing in:
a `thinking_chunk` of a SEAL-4 turn would land in the same file as one of a
SEAL-0 turn, and reasoning quotes the channel's content. Stored reasoning lives
in `agents/reasoning_log.py`, under `agent-state/reasoning/<tier>/<channel>/`,
where the tier is the first directory of the path. Whoever wants to extend the
activity log to `tool_use`/`message_chunk` faces the same problem, and this
paragraph is the warning: tier first, then format.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import data_path
from ..core.events import bus
from ..core.models import Event

LOG = logging.getLogger("agent-server.agents.activity")

ACTIVITY_DIR = data_path("agent-state") / "activity"

# Bucket per i run_done storici (pre-tracciamento provider) senza `payload.provider`.
# Nella leaderboard va SEMPRE in fondo, a prescindere dai token (non è un provider
# reale, non deve competere in cima con quelli attribuiti).
UNKNOWN_PROVIDER = "sconosciuto"


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
    # Root-only from birth (clodia-platform#704): `run_started` and `run_done`
    # carry what was written and answered, and a spawn's shell must not read
    # them. `boundary_check.harden_content_stores` fixes what is already there.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
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


def _usage_totals(usage: dict | None) -> tuple[int, int]:
    """Total input/output tokens da un usage, normalizzato tra provider.

    I provider contano l'input in modo DIVERSO:
    - Anthropic (Claude): `input_tokens` è SOLO il non-cachato; i token di cache
      (`cache_creation_input_tokens` + `cache_read_input_tokens`) sono ADDITIVI →
      vanno sommati, altrimenti l'input è sotto-contato di ordini di grandezza.
    - OpenAI/codex: `input_tokens` è GIÀ il totale (il cached è un sottoinsieme
      informativo) → NON sommare i cache o si raddoppia.
    Discriminante: la presenza di `cache_creation_input_tokens` = stile-Anthropic.
    """
    usage = usage or {}
    tokens_out = int(usage.get("output_tokens", 0) or 0)
    if usage.get("cache_creation_input_tokens") is not None:
        tokens_in = (int(usage.get("input_tokens", 0) or 0)
                     + int(usage.get("cache_creation_input_tokens", 0) or 0)
                     + int(usage.get("cache_read_input_tokens", 0) or 0))
    else:
        tokens_in = int(usage.get("input_tokens", 0) or 0)
    return tokens_in, tokens_out


def _run_done_fingerprint(agent: str, event: dict) -> str:
    """Stable key per ignorare doppie scritture dello stesso completamento.

    Hashata, non letterale: da clodia-platform#208 la `reply` arriva fino a 4000
    caratteri invece di 160, e queste chiavi stanno in un `set` per tutta la
    scansione della leaderboard — la chiave serve a confrontare, non a essere
    letta, quindi tenerla intera sarebbe stato ~25 volte più memoria per niente.
    """
    payload = event.get("payload") or {}
    usage = payload.get("usage") or {}
    raw = {
        "agent": agent,
        "chat_id": payload.get("chat_id"),
        "provider": payload.get("provider") or UNKNOWN_PROVIDER,
        "reply": payload.get("reply"),
        "usage": usage,
        "usage_semantics": payload.get("usage_semantics"),
        "usage_source": payload.get("usage_source"),
    }
    testo = json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(testo.encode("utf-8")).hexdigest()


def summary(agent_names: list[str] | None = None) -> list[dict]:
    """Leaderboard per agent seed con contatori cumulativi all-time.

    `agent_names` permette alla registry di includere anche seed senza log,
    restituendoli con contatori a zero.
    """
    names = set(agent_names or [])
    if ACTIVITY_DIR.is_dir():
        names.update(child.name for child in ACTIVITY_DIR.iterdir() if child.is_dir())
    out = []
    for agent in sorted(names):
        today_runs = 0
        runs = 0
        tokens_in = 0
        tokens_out = 0
        status = "idle"
        last_run_ts = None
        last_event = None
        seen_run_done: set[str] = set()

        for e in _read_jsonl(_file_for(agent)):
            if e.get("type") == "run_started":
                today_runs += 1

        for f in _agent_log_files(agent):
            for e in _read_jsonl(f):
                typ = e.get("type")
                if typ == "run_started":
                    last_run_ts = e.get("ts")
                    status = "running"
                elif typ == "run_done":
                    fp = _run_done_fingerprint(agent, e)
                    if fp in seen_run_done:
                        last_event = e
                        continue
                    seen_run_done.add(fp)
                    runs += 1
                    tin, tout = _usage_totals((e.get("payload") or {}).get("usage"))
                    tokens_in += tin
                    tokens_out += tout
                    status = "idle"
                elif typ == "run_error":
                    status = "error"
                last_event = e

        out.append({
            "agent": agent,
            "today_runs": today_runs,
            "runs": runs,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "status": status,
            "last_run_ts": last_run_ts,
            "last_event_ts": last_event.get("ts") if last_event else None,
            "last_event_type": last_event.get("type") if last_event else None,
        })
    return out


def provider_summary(agent_names: list[str] | None = None) -> list[dict]:
    """Leaderboard cumulativa per PROVIDER di inferenza: token consumati da ogni
    servizio (i prezzi differiscono molto → utile vedere il consumo per provider).

    Il provider di ogni run è quello registrato nell'evento `run_done`
    (`payload.provider`). Gli eventi storici che non lo riportano (pre-feature)
    finiscono in **"sconosciuto"**: NON si indovina il provider corrente
    dell'agente, che sarebbe una mis-attribuzione temporale (un agente può aver
    cambiato provider dopo quegli eventi). Il conteggio token riusa `_usage_totals`
    (normalizza le differenze di conteggio input fra Anthropic e OpenAI)."""
    names = set(agent_names or [])
    if ACTIVITY_DIR.is_dir():
        names.update(child.name for child in ACTIVITY_DIR.iterdir() if child.is_dir())

    acc: dict[str, dict] = {}
    seen_run_done: set[str] = set()
    for agent in sorted(names):
        for f in _agent_log_files(agent):
            for e in _read_jsonl(f):
                if e.get("type") != "run_done":
                    continue
                fp = _run_done_fingerprint(agent, e)
                if fp in seen_run_done:
                    continue
                seen_run_done.add(fp)
                payload = e.get("payload") or {}
                provider = payload.get("provider") or UNKNOWN_PROVIDER
                tin, tout = _usage_totals(payload.get("usage"))
                row = acc.setdefault(provider, {
                    "provider": provider, "runs": 0, "tokens_in": 0, "tokens_out": 0,
                    "agents": set(), "last_event_ts": None,
                })
                row["runs"] += 1
                row["tokens_in"] += tin
                row["tokens_out"] += tout
                row["agents"].add(agent)
                ts = e.get("ts")
                if ts and (row["last_event_ts"] is None or ts > row["last_event_ts"]):
                    row["last_event_ts"] = ts

    out = []
    for row in acc.values():
        row = dict(row)
        row["agents"] = sorted(row.pop("agents"))
        row["agents_count"] = len(row["agents"])
        out.append(row)
    # Provider reali per token desc; "sconosciuto" (storico non attribuito) SEMPRE
    # in fondo. Con reverse=True, `provider != UNKNOWN` dà True(1) ai reali e
    # False(0) allo sconosciuto → i reali precedono, lo sconosciuto chiude.
    out.sort(key=lambda r: (r["provider"] != UNKNOWN_PROVIDER,
                            r["tokens_in"] + r["tokens_out"], r["runs"]),
             reverse=True)
    return out


# ── Usage per topic (clodia-platform#732) ────────────────────────────────────
#: The windows of the Activity page's «Consumo per topic», in seconds.
TOPIC_WINDOWS = {"24h": 86400, "7d": 7 * 86400, "4w": 28 * 86400}
#: The page polls every few seconds; a scan of four weeks of logs is not
#: something to repeat that often.
TOPIC_USAGE_TTL_S = 60.0
_topic_usage_cache: dict = {}


def _room_of(chat_id) -> "tuple[str, str] | None":
    """`chan:SEAL-1:moneyviz-2026:clodia` → `("SEAL-1", "moneyviz-2026")`.
    Job and pack-operation chats have no room: None."""
    parts = str(chat_id or "").split(":")
    if len(parts) >= 3 and parts[0] == "chan" and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None


def _ts(raw) -> "float | None":
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except (TypeError, ValueError):
        return None


def topic_usage(now: "float | None" = None) -> dict:
    """Per room, for each window of `TOPIC_WINDOWS`: turns, seconds, tokens in
    and tokens out. `{(tier, name): {"24h": {...}, "7d": {...}, "4w": {...}}}`.

    A turn is a `run_started` closed by the next `run_done`/`run_error` of the
    same agent in the same chat; its time is the distance between the two and
    it belongs to the window of its END. Tokens come from `run_done`, with the
    leaderboard's normalisation and its de-duplication of double writes. Only
    the files of the last four weeks are read (the name is the date)."""
    import time as _time
    clock = now if now is not None else _time.time()
    hit = _topic_usage_cache.get("v")
    if now is None and hit and clock - hit[0] < TOPIC_USAGE_TTL_S:
        return hit[1]
    horizon = clock - max(TOPIC_WINDOWS.values())
    first_day = datetime.fromtimestamp(horizon - 86400, tz=timezone.utc).strftime("%Y-%m-%d")
    out: dict = {}
    agents = [c.name for c in ACTIVITY_DIR.iterdir() if c.is_dir()] if ACTIVITY_DIR.is_dir() else []
    for agent in agents:
        open_turns: dict = {}
        seen: set[str] = set()
        for f in _agent_log_files(agent):
            if f.stem < first_day:
                continue
            for e in _read_jsonl(f):
                typ = e.get("type")
                if typ not in ("run_started", "run_done", "run_error"):
                    continue
                payload = e.get("payload") or {}
                room = _room_of(payload.get("chat_id"))
                if room is None:
                    continue
                t = _ts(e.get("ts"))
                if t is None:
                    continue
                key = payload.get("chat_id")
                if typ == "run_started":
                    open_turns[key] = t
                    continue
                if typ == "run_done":
                    fp = _run_done_fingerprint(agent, e)
                    if fp in seen:
                        continue
                    seen.add(fp)
                started = open_turns.pop(key, None)
                if t < horizon:
                    continue
                secs = max(0.0, t - started) if started is not None else 0.0
                tin, tout = _usage_totals(payload.get("usage")) if typ == "run_done" else (0, 0)
                per = out.setdefault(room, {w: {"runs": 0, "seconds": 0.0, "tokens_in": 0,
                                                "tokens_out": 0} for w in TOPIC_WINDOWS})
                for w, span in TOPIC_WINDOWS.items():
                    if t >= clock - span:
                        b = per[w]
                        b["runs"] += 1
                        b["seconds"] += secs
                        b["tokens_in"] += tin
                        b["tokens_out"] += tout
    for per in out.values():
        for b in per.values():
            b["seconds"] = round(b["seconds"])
    if now is None:
        _topic_usage_cache["v"] = (clock, out)
    return out
