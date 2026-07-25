"""Feedback supervisionato del routing (few-shot per il router).

Conferme e correzioni sono segnali sulla SCELTA dell'agente, non sulla qualità
della sua risposta. Salviamo l'embedding del messaggio (mai il testo) e il target
corretto per il few-shot k-NN, più il tipo di segnale per poter mantenere score
di selezione separati dalle lesson dell'agente.
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
    record_feedback(embedding, kind="correction", chosen_agent=router_chose,
                    correct_agent=correct_agent, tier=tier, by=by)


def record_confirmation(embedding: list[float], chosen_agent: str,
                        tier: str | None = None, by: str | None = None) -> None:
    record_feedback(embedding, kind="confirm", chosen_agent=chosen_agent,
                    tier=tier, by=by)


def record_feedback(embedding: list[float], *, kind: str,
                    chosen_agent: str | None,
                    correct_agent: str | None = None,
                    tier: str | None = None, by: str | None = None) -> None:
    global _CACHE
    if kind not in {"confirm", "correction"}:
        raise ValueError("kind deve essere confirm o correction")
    target_agent = chosen_agent if kind == "confirm" else correct_agent
    if not embedding or not target_agent:
        return
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now(timezone.utc).isoformat(),
           "kind": kind, "agent": target_agent, "router_chose": chosen_agent,
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
    try:
        rows = [json.loads(ln) for ln in _FILE.read_text("utf-8").splitlines() if ln.strip()]
    except FileNotFoundError:
        rows = []
    scores: dict[str, dict[str, int]] = {}
    confirmations = corrections = 0
    for row in rows:
        kind = row.get("kind", "correction")  # compatibilità record legacy
        target = row.get("agent")
        chosen = row.get("router_chose")
        if kind == "confirm":
            confirmations += 1
            if target:
                score = scores.setdefault(target, {"confirmed": 0, "corrected": 0, "score": 0})
                score["confirmed"] += 1
                score["score"] += 1
        else:
            corrections += 1
            if chosen and chosen != target:
                score = scores.setdefault(chosen, {"confirmed": 0, "corrected": 0, "score": 0})
                score["corrected"] += 1
                score["score"] -= 1
    return {
        "total": len(rows),
        "total_confirmations": confirmations,
        "total_corrections": corrections,
        "selection_scores": scores,
    }
