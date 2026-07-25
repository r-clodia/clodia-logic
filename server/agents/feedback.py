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
_MEMORY_FILE = "MEMORY.md"
_LESSONS_START = "<!-- clodia:feedback-lessons:start -->"
_LESSONS_END = "<!-- clodia:feedback-lessons:end -->"


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


def _replace_managed_block(body: str, block: str) -> str:
    start = body.find(_LESSONS_START)
    end = body.find(_LESSONS_END)
    if start >= 0 and end >= start:
        end += len(_LESSONS_END)
        merged = body[:start].rstrip() + "\n\n" + block + body[end:]
    else:
        merged = body.rstrip() + ("\n\n" if body.strip() else "") + block
    return merged.rstrip() + "\n"


def _sync_memory(path: Path, rows: list[dict]) -> Path:
    """Rende MEMORY.md la fonte leggibile delle lesson apprese.

    Il JSON resta il registro strutturato per stato, audit e API, ma il prompt
    legge esclusivamente MEMORY.md.
    """
    learned = [
        row for row in rows
        if row.get("status") == "learned" and str(row.get("lesson") or "").strip()
    ]
    lines = [
        f"- <!-- feedback:{row.get('id', '')} --> {str(row['lesson']).strip()}"
        for row in learned
    ]
    content = (
        f"{_LESSONS_START}\n"
        "## Lesson learned dal feedback umano\n\n"
        + ("\n".join(lines) if lines else "_Nessuna lesson appresa._")
        + f"\n{_LESSONS_END}"
    )
    memory_path = path.with_name(_MEMORY_FILE)
    previous = (
        memory_path.read_text(encoding="utf-8")
        if memory_path.is_file()
        else "# Memory Index\n"
    )
    tmp = memory_path.with_suffix(".tmp")
    tmp.write_text(_replace_managed_block(previous, content), encoding="utf-8")
    tmp.replace(memory_path)
    return memory_path


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
        _sync_memory(path, rows)
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
        _sync_memory(path, kept)
        return True


def prompt_section(agent: str, *, limit: int = 30) -> str:
    """Lesson apprese da inserire nel contesto dei futuri workspace."""
    return prompt_section_for_spec(registry.get_by_name(agent), limit=limit)


def prompt_section_for_spec(spec, *, limit: int = 30) -> str:
    """MEMORY.md da inserire nel prompt; il JSON non è mai letto direttamente."""
    if spec is None or not getattr(spec, "agent_dir", None):
        return ""
    mem_rel = spec.memory.dir if getattr(spec, "memory", None) else "memory/"
    path = Path(spec.agent_dir) / mem_rel / _FILE
    with _LOCK:
        memory_path = _sync_memory(path, _read(path))
        body = memory_path.read_text(encoding="utf-8").strip()
    if not body:
        return ""
    return "## Memoria persistente del seed\n\n" + body
