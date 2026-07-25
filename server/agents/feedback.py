"""Feedback umano sugli output e lesson learned persistenti per-agent."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import registry

_FILE = "feedback-lessons.json"
_LOCK = threading.Lock()


def _path(agent: str) -> Path:
    spec = registry.get_by_name(agent)
    if spec is None:
        raise KeyError(agent)
    mem_rel = spec.memory.dir if spec.memory else "memory/"
    path = Path(spec.agent_dir) / mem_rel / _FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read(path: Path) -> list[dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return raw if isinstance(raw, list) else []


def _write(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def create(*, agent: str, message_id: str, topic: str, rating: str,
           by: str, comment: str = "") -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "message_id": message_id,
        "topic": topic,
        "rating": rating,
        "comment": comment.strip()[:1000],
        "by": by,
        "status": "pending",
        "lesson": None,
    }
    path = _path(agent)
    with _LOCK:
        rows = _read(path)
        rows.append(row)
        _write(path, rows)
    return row


def complete(agent: str, lesson_id: str, lesson: str) -> dict | None:
    path = _path(agent)
    with _LOCK:
        rows = _read(path)
        found = next((r for r in rows if r.get("id") == lesson_id), None)
        if found is None:
            return None
        found["lesson"] = lesson.strip()[:4000]
        found["status"] = "learned"
        found["learned_at"] = datetime.now(timezone.utc).isoformat()
        _write(path, rows)
        return found


def fail(agent: str, lesson_id: str, detail: str) -> None:
    path = _path(agent)
    with _LOCK:
        rows = _read(path)
        found = next((r for r in rows if r.get("id") == lesson_id), None)
        if found is None:
            return
        found["status"] = "error"
        found["error"] = detail[:300]
        _write(path, rows)


def list_for(agent: str, *, topic: str | None = None) -> list[dict]:
    rows = _read(_path(agent))
    if topic:
        rows = [r for r in rows if r.get("topic") == topic]
    return list(reversed(rows))


def delete(agent: str, lesson_id: str) -> bool:
    path = _path(agent)
    with _LOCK:
        rows = _read(path)
        kept = [r for r in rows if r.get("id") != lesson_id]
        if len(kept) == len(rows):
            return False
        _write(path, kept)
        return True


def prompt_section(agent: str, *, limit: int = 30) -> str:
    """Lesson apprese da inserire nel contesto dei futuri workspace."""
    return prompt_section_for_spec(registry.get_by_name(agent), limit=limit)


def prompt_section_for_spec(spec, *, limit: int = 30) -> str:
    """Variante usabile durante la materializzazione, anche nei test con spec ad hoc."""
    if spec is None or not getattr(spec, "agent_dir", None):
        return ""
    mem_rel = spec.memory.dir if getattr(spec, "memory", None) else "memory/"
    learned = [
        r for r in _read(Path(spec.agent_dir) / mem_rel / _FILE)
        if r.get("status") == "learned" and r.get("lesson")
    ]
    if not learned:
        return ""
    lines = "\n".join(f"- {r['lesson'].strip()}" for r in reversed(learned[:limit]))
    return (
        "## Lesson learned dal feedback umano\n\n"
        "Applica queste indicazioni alle risposte future quando pertinenti:\n\n"
        f"{lines}"
    )
