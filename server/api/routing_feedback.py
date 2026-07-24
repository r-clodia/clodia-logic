"""Correzioni supervisionate del routing (few-shot per il router).

Quando l'utente indica CHI avrebbe fatto rispondere, salviamo un ESEMPIO:
l'EMBEDDING del messaggio instradato (NON il testo → privacy: i topic possono
essere confidenziali) + l'agente corretto. Al routing successivo, un messaggio molto
simile a un esempio viene instradato all'agente corretto (override k-NN) → il router
impara dalle correzioni. Persistito append-only in CLODIA_DATA/routing/corrections.jsonl.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..config import data_path

_FILE = data_path("routing") / "corrections.jsonl"

# cache in-memory degli esempi (ricaricata su record)
_CACHE: list[dict] | None = None


def record_correction(embedding: list[float], correct_agent: str,
                      router_chose: str | None = None, tier: str | None = None,
                      by: str | None = None) -> None:
    global _CACHE
    if not embedding or not correct_agent:
        return
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now(timezone.utc).isoformat(),
           "agent": correct_agent, "router_chose": router_chose,
           "tier": tier, "by": by, "vec": [round(float(x), 6) for x in embedding]}
    with _FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    _CACHE = None  # invalida


def load_exemplars() -> list[dict]:
    """[{agent, vec}] da disco (cache in-memory)."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        rows = [json.loads(ln) for ln in _FILE.read_text("utf-8").splitlines() if ln.strip()]
    except FileNotFoundError:
        rows = []
    _CACHE = [{"agent": r.get("agent"), "vec": r.get("vec")} for r in rows
             if r.get("agent") and r.get("vec")]
    return _CACHE


def stats() -> dict:
    ex = load_exemplars()
    per_agent: dict[str, int] = {}
    for e in ex:
        per_agent[e["agent"]] = per_agent.get(e["agent"], 0) + 1
    return {"total_corrections": len(ex), "per_agent": per_agent}
