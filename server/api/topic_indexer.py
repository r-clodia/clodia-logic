"""The topic indexer's driver (clodia-platform#601).

The gateway indexes (`/internal/topic-index/{tier}/{name}` sync: extraction,
provenance, the RAG service's token stay there). This loop only decides the
PACE: a few topics per cycle, round-robin, topics with work left first, so
the backfill of every existing topic spreads over hours instead of one burst
on the minipc. Deterministic code, never a model.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

import requests

from ..config import data_path
from . import topics_client

LOG = logging.getLogger("agent-server.topic_indexer")

PER_CYCLE = int(os.environ.get("CLODIA_TOPIC_INDEX_PER_CYCLE", "3"))
_TIMEOUT = 600


def _base() -> str:
    return topics_client._base().rsplit("/topics", 1)[0] + "/topic-index"


def topics() -> list[dict]:
    r = requests.get(_base(), headers=topics_client._headers(), timeout=60)
    r.raise_for_status()
    return list(r.json().get("topics") or [])


def sync(tier: str, name: str) -> dict:
    r = requests.post(f"{_base()}/{tier}/{name}", headers=topics_client._headers(),
                      json={"action": "sync"}, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"sync {tier}/{name}: HTTP {r.status_code}")
    return r.json()


def _state_file():
    p = data_path("topic-indexer")
    p.mkdir(parents=True, exist_ok=True)
    return p / "state.json"


def _load() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"next": 0, "pending": []}


def _save(st: dict) -> None:
    _state_file().write_text(json.dumps(st), encoding="utf-8")


def pick(all_topics: list[dict], st: dict, n: int = PER_CYCLE) -> list[dict]:
    """Topics with work left first (`pending`), then round-robin from `next`."""
    keys = [f"{t['tier']}/{t['name']}" for t in all_topics]
    by_key = dict(zip(keys, all_topics))
    chosen = [by_key[k] for k in st.get("pending") or [] if k in by_key][:n]
    if keys and len(chosen) < n:
        start = int(st.get("next") or 0) % len(keys)
        for i in range(len(keys)):
            k = keys[(start + i) % len(keys)]
            if by_key[k] not in chosen:
                chosen.append(by_key[k])
                st["next"] = (start + i + 1) % len(keys)
            if len(chosen) >= n:
                break
    return chosen


async def run_cycle() -> int:
    try:
        tutti = await asyncio.to_thread(topics)
    except Exception as e:  # noqa: BLE001
        LOG.warning("topic indexer: topics unavailable (%s)", type(e).__name__)
        return 0
    st = _load()
    pending = set(st.get("pending") or [])
    fatti = 0
    for t in pick(tutti, st):
        key = f"{t['tier']}/{t['name']}"
        try:
            out = await asyncio.to_thread(sync, t["tier"], t["name"])
        except Exception as e:  # noqa: BLE001 — one topic never stops the others
            LOG.warning("topic indexer: %s not synced (%s)", key, str(e)[:120])
            continue
        fatti += 1
        if out.get("pending"):
            pending.add(key)
        else:
            pending.discard(key)
        n = int(out.get("days") or 0) + int(out.get("files") or 0) + int(out.get("removed") or 0)
        if n:
            LOG.info("topic indexer: %s → %s", key,
                     {k: out.get(k) for k in ("days", "files", "removed")})
    st["pending"] = sorted(pending)
    _save(st)
    return fatti
