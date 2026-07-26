"""Runtime-only agent overrides scoped to a topic, chat, or async run.

The store is deliberately separate from ``agent.yaml``. Records are short-lived,
auditable, and only created after the API verifies a CA-signed M-gate approval.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from .config import data_path

VALID_SCOPES = frozenset({"topic", "chat", "run"})
MAX_TTL_MINUTES = 120
_LOCK = threading.RLock()


def _path() -> Path:
    return data_path("runtime/scoped-agent-overrides.json")


def _approval_path() -> Path:
    return data_path("runtime/scoped-agent-approval-jtis.json")


def _load_unlocked() -> list[dict[str, Any]]:
    path = _path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _save_unlocked(rows: list[dict[str, Any]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)


def _live_unlocked(now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    rows = _load_unlocked()
    live = [row for row in rows if float(row.get("expires_at") or 0) > now]
    if len(live) != len(rows):
        _save_unlocked(live)
    return live


def topic_from_chat_id(chat_id: str | None) -> str | None:
    """Return ``tier/name`` for a channel/DM chat id."""
    if not chat_id or not chat_id.startswith("chan:"):
        return None
    parts = chat_id.split(":")
    if len(parts) < 4 or not parts[1] or not parts[2]:
        return None
    return f"{parts[1]}/{parts[2]}"


def normalize_scope(kind: str, scope_id: str) -> tuple[str, str]:
    kind = (kind or "").strip().lower()
    scope_id = (scope_id or "").strip()
    if kind not in VALID_SCOPES:
        raise ValueError("scope_kind deve essere topic, chat o run")
    if not scope_id or len(scope_id) > 240:
        raise ValueError("scope_id richiesto (max 240 caratteri)")
    if kind == "topic":
        parts = scope_id.split("/", 1)
        if len(parts) != 2 or not all(parts):
            raise ValueError("scope topic atteso come tier/name")
    return kind, scope_id


def consume_approval(jti: str, expires_at: float) -> bool:
    """Record a signed gate JTI once, retaining it until the capability expires."""
    jti = (jti or "").strip()
    if not jti:
        return False
    with _LOCK:
        path = _approval_path()
        try:
            used = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            used = {}
        now = time.time()
        used = {key: exp for key, exp in used.items() if float(exp or 0) > now}
        if jti in used:
            return False
        used[jti] = float(expires_at)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(used, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        return True


def create(
    *,
    agent: str,
    scope_kind: str,
    scope_id: str,
    ttl_minutes: int,
    requested_by: str,
    approved_by: str,
    approval_jti: str,
    reason: str = "",
    capabilities: list[str] | None = None,
    rules: list[str] | None = None,
    tools: list[str] | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    scope_kind, scope_id = normalize_scope(scope_kind, scope_id)
    ttl = max(1, min(int(ttl_minutes or 15), MAX_TTL_MINUTES))
    now = time.time()
    row = {
        "id": secrets.token_hex(8),
        "agent": agent,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "capabilities": list(dict.fromkeys(capabilities or [])),
        "rules": list(dict.fromkeys(rules or [])),
        "tools": list(dict.fromkeys(tools or [])),
        "model": (model or "").strip() or None,
        "provider": (provider or "").strip() or None,
        "reason": (reason or "").strip()[:500],
        "requested_by": requested_by,
        "approved_by": approved_by,
        "approval_jti": approval_jti,
        "created_at": now,
        "expires_at": now + ttl * 60,
    }
    if not any(
        (row["capabilities"], row["rules"], row["tools"], row["model"], row["provider"])
    ):
        raise ValueError("override vuoto")
    with _LOCK:
        rows = _live_unlocked(now)
        rows.append(row)
        _save_unlocked(rows)
    return dict(row)


def list_active(agent: str | None = None) -> list[dict[str, Any]]:
    with _LOCK:
        rows = _live_unlocked()
    if agent:
        rows = [row for row in rows if row.get("agent") == agent]
    return sorted(rows, key=lambda row: float(row.get("created_at") or 0), reverse=True)


def revoke(override_id: str, *, agent: str | None = None) -> dict[str, Any] | None:
    with _LOCK:
        rows = _live_unlocked()
        removed = next(
            (
                row
                for row in rows
                if row.get("id") == override_id
                and (not agent or row.get("agent") == agent)
            ),
            None,
        )
        if removed is None:
            return None
        _save_unlocked([row for row in rows if row is not removed])
    return dict(removed)


def _matches(
    row: dict[str, Any],
    *,
    chat_id: str | None,
    run_id: str | None,
) -> bool:
    kind = row.get("scope_kind")
    scope_id = row.get("scope_id")
    if kind == "chat":
        return bool(chat_id) and scope_id == chat_id
    if kind == "topic":
        return scope_id == topic_from_chat_id(chat_id)
    if kind == "run":
        return bool(run_id) and scope_id == run_id
    return False


def resolve(
    agent: str,
    *,
    chat_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Merge matching records; newest scalar model/provider wins."""
    rows = [
        row
        for row in reversed(list_active(agent))
        if _matches(row, chat_id=chat_id, run_id=run_id)
    ]
    out: dict[str, Any] = {
        "capabilities": [],
        "rules": [],
        "tools": [],
        "model": None,
        "provider": None,
        "records": [],
    }
    for row in rows:
        for field in ("capabilities", "rules", "tools"):
            for value in row.get(field) or []:
                if value not in out[field]:
                    out[field].append(value)
        for field in ("model", "provider"):
            if row.get(field):
                out[field] = row[field]
        out["records"].append(
            {
                key: row.get(key)
                for key in (
                    "id",
                    "scope_kind",
                    "scope_id",
                    "reason",
                    "requested_by",
                    "approved_by",
                    "created_at",
                    "expires_at",
                )
            }
        )
    return out
