"""Langfuse tracing helpers for agent-server.

Secrets are loaded by the server process from files under ``secrets/`` and are
never logged or returned by API endpoints. The module is intentionally optional:
missing credentials or a missing Langfuse SDK must not prevent Clodia from
starting.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import workspace_path

LOG = logging.getLogger("agent-server.observability")

_CONFIGURED = False
_CLIENT: Any = None
_WARNED_MISSING_SDK = False
_WARNED_DISABLED = False

_SECRET_FILES = {
    "LANGFUSE_PUBLIC_KEY": "langfuse-public",
    "LANGFUSE_SECRET_KEY": "langfuse-secret",
    "LANGFUSE_BASE_URL": "langfuse-baseurl",
}


class _NoopObservation:
    def update(self, **_: Any) -> None:
        return None

    def end(self, **_: Any) -> None:
        return None


def _read_secret_file(path: Path) -> Optional[str]:
    """Read a secret value without logging or exposing it."""
    try:
        if not path.is_file():
            return None
        value = path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        LOG.warning("Langfuse secret file unreadable: %s (%s)", path.name, exc)
        return None
    return value or None


def configure_langfuse_env() -> bool:
    """Populate Langfuse env vars from secret files if needed.

    Returns True when the minimum configuration is present. ``LANGFUSE_HOST`` is
    also populated for SDK/tooling compatibility with older integrations.
    """
    global _CONFIGURED, _WARNED_DISABLED
    if _CONFIGURED:
        return _has_required_env()

    secrets_dir = workspace_path("secrets")
    for env_name, filename in _SECRET_FILES.items():
        if os.environ.get(env_name):
            continue
        value = _read_secret_file(secrets_dir / filename)
        if value:
            os.environ[env_name] = value

    if os.environ.get("LANGFUSE_BASE_URL") and not os.environ.get("LANGFUSE_HOST"):
        os.environ["LANGFUSE_HOST"] = os.environ["LANGFUSE_BASE_URL"]
    if os.environ.get("LANGFUSE_HOST") and not os.environ.get("LANGFUSE_BASE_URL"):
        os.environ["LANGFUSE_BASE_URL"] = os.environ["LANGFUSE_HOST"]

    _CONFIGURED = True
    ok = _has_required_env()
    if not ok and not _WARNED_DISABLED:
        LOG.info("Langfuse tracing disabled: missing SDK credentials or base URL")
        _WARNED_DISABLED = True
    return ok


def _has_required_env() -> bool:
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY")
        and os.environ.get("LANGFUSE_SECRET_KEY")
        and (os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST"))
    )


def get_langfuse_client() -> Any | None:
    """Return the Langfuse client, or None when tracing is unavailable."""
    global _CLIENT, _WARNED_MISSING_SDK
    if os.environ.get("LANGFUSE_TRACING_ENABLED", "true").lower() in {"0", "false", "no"}:
        return None
    if not configure_langfuse_env():
        return None
    if _CLIENT is not None:
        return _CLIENT
    try:
        # Import after env configuration: Langfuse initializes from env.
        from langfuse import get_client
    except ImportError:
        if not _WARNED_MISSING_SDK:
            LOG.warning("Langfuse tracing disabled: python package 'langfuse' is not installed")
            _WARNED_MISSING_SDK = True
        return None
    _CLIENT = get_client()
    return _CLIENT


@contextmanager
def langfuse_observation(
    *,
    name: str,
    as_type: str = "span",
    input: Any | None = None,
    model: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Create a Langfuse observation or yield a no-op object."""
    client = get_langfuse_client()
    if client is None:
        yield _NoopObservation()
        return

    kwargs: dict[str, Any] = {
        "name": name,
        "as_type": as_type,
    }
    if input is not None:
        kwargs["input"] = input
    if model:
        kwargs["model"] = model
    if metadata:
        kwargs["metadata"] = _clean_metadata(metadata)

    with client.start_as_current_observation(**kwargs) as observation:
        yield observation


@contextmanager
def langfuse_attributes(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    trace_name: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[None]:
    """Propagate trace attributes when Langfuse is configured."""
    if get_langfuse_client() is None:
        yield
        return
    from langfuse import propagate_attributes

    kwargs: dict[str, Any] = {}
    if session_id:
        kwargs["session_id"] = _short_str(session_id)
    if user_id:
        kwargs["user_id"] = _short_str(user_id)
    if trace_name:
        kwargs["trace_name"] = _short_str(trace_name)
    if tags:
        kwargs["tags"] = [_short_str(tag) for tag in tags if tag]
    if metadata:
        kwargs["metadata"] = _clean_metadata(metadata)

    with propagate_attributes(**kwargs):
        yield


def flush_langfuse() -> None:
    client = get_langfuse_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception as exc:
        LOG.warning("Langfuse flush failed: %s", exc)


def trace_io(value: str | None) -> dict[str, Any]:
    """Return privacy-conscious IO metadata.

    Full content capture is opt-in because Clodia may process confidential
    material. Set LANGFUSE_CAPTURE_CONTENT=true to include text payloads.
    """
    text = value or ""
    out: dict[str, Any] = {"chars": len(text)}
    if os.environ.get("LANGFUSE_CAPTURE_CONTENT", "false").lower() in {"1", "true", "yes"}:
        out["content"] = text
    return out


def _clean_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in metadata.items():
        safe_key = "".join(ch for ch in str(key) if ch.isalnum() or ch == "_")
        if not safe_key:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[safe_key] = _short_str(value) if isinstance(value, str) else value
        else:
            clean[safe_key] = _short_str(str(value))
    return clean


def _short_str(value: str, limit: int = 200) -> str:
    return value[:limit]
