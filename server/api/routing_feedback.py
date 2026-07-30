"""Feedback supervisionato del routing (few-shot per il router).

Conferme e correzioni sono segnali sulla SCELTA dell'agente, non sulla qualità
della sua risposta. Salviamo l'embedding del messaggio (mai il testo) e il target
corretto per il few-shot k-NN, più il tipo di segnale per poter mantenere score
di selezione separati dalle lesson dell'agente.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone

from ..config import data_path

_FILE = data_path("routing") / "corrections.jsonl"
_DECISIONS_FILE = data_path("routing") / "decisions.jsonl"
LOG = logging.getLogger("agent-server.routing_feedback")

# cache in-memory degli esempi (ricaricata su record)
_CACHE: list[dict] | None = None
_CACHE_KEY: frozenset[str] | None = None
_WRITE_LOCK = threading.Lock()


def record_correction(embedding: list[float], correct_agent: str,
                      router_chose: str | None = None, tier: str | None = None,
                      by: str | None = None, topic: str | None = None) -> None:
    record_feedback(embedding, kind="correction", chosen_agent=router_chose,
                    correct_agent=correct_agent, tier=tier, by=by, topic=topic)


def record_confirmation(embedding: list[float], chosen_agent: str,
                        tier: str | None = None, by: str | None = None,
                        topic: str | None = None) -> None:
    record_feedback(embedding, kind="confirm", chosen_agent=chosen_agent,
                    tier=tier, by=by, topic=topic)


def _topic_hash(topic: str | None) -> str | None:
    if not topic:
        return None
    return hashlib.sha256(topic.encode("utf-8")).hexdigest()[:16]


def _latest_decision_origin(topic: str | None,
                            chosen_agent: str | None) -> str | None:
    topic_hash = _topic_hash(topic)
    if not topic_hash or not chosen_agent:
        return None
    for row in reversed(_read_jsonl(_DECISIONS_FILE)):
        if (
            row.get("topic_hash") == topic_hash
            and chosen_agent in (row.get("chosen") or [])
        ):
            return row.get("origin")
    return None


def record_feedback(embedding: list[float], *, kind: str,
                    chosen_agent: str | None,
                    correct_agent: str | None = None,
                    tier: str | None = None, by: str | None = None,
                    topic: str | None = None) -> None:
    global _CACHE, _CACHE_KEY
    if kind not in {"confirm", "correction"}:
        raise ValueError("kind deve essere confirm o correction")
    target_agent = chosen_agent if kind == "confirm" else correct_agent
    if not embedding or not target_agent:
        return
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now(timezone.utc).isoformat(),
           "kind": kind, "agent": target_agent, "router_chose": chosen_agent,
           "tier": tier, "by": by,
           "decision_origin": _latest_decision_origin(topic, chosen_agent),
           "vec": [round(float(x), 6) for x in embedding]}
    with _WRITE_LOCK, _FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    _CACHE = None  # invalida
    _CACHE_KEY = None


def _read_jsonl(path) -> list[dict]:
    try:
        return [
            json.loads(line)
            for line in path.read_text("utf-8").splitlines()
            if line.strip()
        ]
    except FileNotFoundError:
        return []


def _write_jsonl_atomic(path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = "".join(
        json.dumps(row, ensure_ascii=False) + "\n" for row in rows
    )
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def load_exemplars(known_agents: set[str] | None = None) -> list[dict]:
    """Esemplari completi, con pulizia atomica dei target non più registrati."""
    global _CACHE, _CACHE_KEY
    known = frozenset(known_agents or ())
    cache_key = known if known else None
    if _CACHE is not None and _CACHE_KEY == cache_key:
        return _CACHE

    rows = _read_jsonl(_FILE)
    if known:
        with _WRITE_LOCK:
            rows = _read_jsonl(_FILE)
            kept = [row for row in rows if row.get("agent") in known]
            removed = len(rows) - len(kept)
            if removed:
                _write_jsonl_atomic(_FILE, kept)
                LOG.info("routing exemplars: rimossi %d target orfani", removed)
        rows = kept

    _CACHE = [
        {
            "agent": row.get("agent"),
            "vec": row.get("vec"),
            "kind": row.get("kind", "correction"),
            "ts": row.get("ts"),
        }
        for row in rows
        if row.get("agent") and row.get("vec")
    ]
    _CACHE_KEY = cache_key
    return _CACHE


def record_decision(origin: str, chosen: str | list[str], *,
                    confidence: float | None = None,
                    mode: str | None = None,
                    topic: str | None = None) -> None:
    """Persist a privacy-preserving routing decision for effectiveness metrics."""
    agents = [chosen] if isinstance(chosen, str) else list(chosen)
    agents = [agent for agent in agents if agent]
    if not agents:
        return
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "origin": origin,
        "chosen": agents,
        "mode": mode,
        "confidence": confidence,
        "topic_hash": _topic_hash(topic),
    }
    _DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK, _DECISIONS_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def stats() -> dict:
    rows = _read_jsonl(_FILE)
    decisions = _read_jsonl(_DECISIONS_FILE)
    scores: dict[str, dict[str, int]] = {}
    confirmations = corrections = 0
    feedback_by_origin: dict[str, dict[str, int | float | None]] = {}
    for row in rows:
        kind = row.get("kind", "correction")  # compatibilità record legacy
        target = row.get("agent")
        chosen = row.get("router_chose")
        decision_origin = row.get("decision_origin")
        if decision_origin:
            origin_stats = feedback_by_origin.setdefault(
                decision_origin,
                {"confirmations": 0, "corrections": 0, "total": 0},
            )
            origin_stats["total"] += 1
            key = "confirmations" if kind == "confirm" else "corrections"
            origin_stats[key] += 1
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
    origins: dict[str, int] = {}
    agents: dict[str, int] = {}
    for row in decisions:
        origin = row.get("origin") or "unknown"
        origins[origin] = origins.get(origin, 0) + 1
        for agent in row.get("chosen") or []:
            agents[agent] = agents.get(agent, 0) + 1
    decision_total = len(decisions)
    for origin_stats in feedback_by_origin.values():
        total = origin_stats["total"]
        origin_stats["accuracy"] = (
            round(origin_stats["confirmations"] / total, 4) if total else None
        )
    return {
        "total": len(rows),
        "total_confirmations": confirmations,
        "total_corrections": corrections,
        "selection_scores": scores,
        "decision_total": decision_total,
        "decisions_by_origin": origins,
        "decisions_by_agent": agents,
        "feedback_by_origin": feedback_by_origin,
        "exemplar_decision_share": (
            round(origins.get("exemplar", 0) / decision_total, 4)
            if decision_total else 0.0
        ),
        "last_decision_at": decisions[-1].get("ts") if decisions else None,
    }
