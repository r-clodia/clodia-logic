"""Kill-switch per agent: pause/resume di tutte le istanze di un agent.

owner 2026-06-06: quando un agent va in loop o si comporta male, deve
esserci un modo immediato di sospenderlo. Quando un agent è in pausa:

- Il `skill_consumer` non lo claima più (skip al prossimo poll).
- Le istanze già running di quell'agent vengono cancellate (asyncio.Task).
- Lo stato è persistente: sopravvive a restart del consumer/container.

State file: `agent-state/paused-agents.json` con shape:
    {"paused": ["aida", "elia", ...]}
"""
from __future__ import annotations
import asyncio
import json
import logging
from typing import Optional

from ..config import data_path

LOG = logging.getLogger("agent-server.agents.pause")

STATE_FILE = data_path("agent-state") / "paused-agents.json"

# Registry in-memory dei task asyncio attivi per agent name.
# Permette il cancel "fan-out" alla pause.
_active_tasks: dict[str, set[asyncio.Task]] = {}


def _load() -> set[str]:
    if not STATE_FILE.is_file():
        return set()
    try:
        return set((json.loads(STATE_FILE.read_text()) or {}).get("paused", []))
    except Exception as e:
        LOG.warning("paused-agents.json corrotto, reset: %s", e)
        return set()


def _save(paused: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"paused": sorted(paused)}, indent=2))


def is_paused(agent_name: str) -> bool:
    return agent_name in _load()


def list_paused() -> list[str]:
    return sorted(_load())


def pause(agent_name: str) -> dict:
    paused = _load()
    was_paused = agent_name in paused
    paused.add(agent_name)
    _save(paused)
    cancelled = 0
    for task in list(_active_tasks.get(agent_name, set())):
        if not task.done():
            task.cancel()
            cancelled += 1
    LOG.warning(
        "Agent '%s' PAUSED (was_paused=%s, cancelled_running=%d)",
        agent_name, was_paused, cancelled,
    )
    return {"paused": True, "cancelled_tasks": cancelled, "was_paused": was_paused}


def resume(agent_name: str) -> dict:
    paused = _load()
    was_paused = agent_name in paused
    paused.discard(agent_name)
    _save(paused)
    LOG.info("Agent '%s' RESUMED (was_paused=%s)", agent_name, was_paused)
    return {"paused": False, "was_paused": was_paused}


def register_task(agent_name: str, task: asyncio.Task) -> None:
    _active_tasks.setdefault(agent_name, set()).add(task)


def unregister_task(agent_name: str, task: asyncio.Task) -> None:
    bucket = _active_tasks.get(agent_name)
    if bucket is None:
        return
    bucket.discard(task)
    if not bucket:
        del _active_tasks[agent_name]
