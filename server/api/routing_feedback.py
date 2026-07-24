"""Feedback supervisionato sul routing del risponditore (voti 👍/👎).

Ogni voto cattura il CONTESTO della decisione — scelto, modalità, punteggi dei
candidati, verdetto — SENZA il testo del messaggio (privacy: i topic possono essere
confidenziali; per il tuning di soglia/margine bastano gli score + il verdetto).
Persistito append-only in CLODIA_DATA/routing/votes.jsonl.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..config import data_path

_FILE = data_path("routing") / "votes.jsonl"


def record(vote: dict) -> None:
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now(timezone.utc).isoformat(), **vote}
    with _FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load() -> list[dict]:
    try:
        return [json.loads(ln) for ln in _FILE.read_text("utf-8").splitlines() if ln.strip()]
    except FileNotFoundError:
        return []


def stats() -> dict:
    votes = load()
    up = sum(1 for v in votes if v.get("verdict") == "up")
    down = sum(1 for v in votes if v.get("verdict") == "down")
    per_agent: dict[str, dict[str, int]] = {}
    for v in votes:
        a = v.get("chosen") or "?"
        d = per_agent.setdefault(a, {"up": 0, "down": 0})
        d[v.get("verdict", "?")] = d.get(v.get("verdict", "?"), 0) + 1
    return {"total": len(votes), "up": up, "down": down, "per_agent": per_agent}
