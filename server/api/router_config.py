"""Live configuration for semantic responder routing.

Versioned defaults live in ``catalogs/router.yaml``. An instance can override
any value in ``CLODIA_DATA/routing/router.yaml``; the file is checked on every
routing decision and reparsed only when its metadata changes.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path

import yaml

from ..config import data_path, workspace_path

LOG = logging.getLogger("agent-server.router_config")

_REPO_PATH = workspace_path("catalogs/router.yaml")
_LIVE_PATH = data_path("routing/router.yaml")


@dataclass(frozen=True)
class RouterConfig:
    recent_messages: int = 3
    threshold: float = 0.80
    margin: float = 0.015


_CACHE_KEY: tuple | None = None
_CACHE = RouterConfig()


def _signature(path: Path) -> tuple[str, int | None, int | None, int | None]:
    try:
        stat = path.stat()
        return str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino
    except OSError:
        return str(path), None, None, None


def _read(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except (OSError, yaml.YAMLError) as exc:
        LOG.warning("router config %s illeggibile (%s): ignorata", path, exc)
        return {}
    if not isinstance(raw, dict):
        LOG.warning("router config %s non e' una mappa: ignorata", path)
        return {}
    return raw


def _validated(raw: dict) -> RouterConfig:
    defaults = RouterConfig()
    try:
        recent_messages = int(raw.get("recent_messages", defaults.recent_messages))
        threshold = float(raw.get("threshold", defaults.threshold))
        margin = float(raw.get("margin", defaults.margin))
    except (TypeError, ValueError) as exc:
        raise ValueError("N, threshold e margin devono essere numerici") from exc
    if not 1 <= recent_messages <= 50:
        raise ValueError("recent_messages deve essere compreso tra 1 e 50")
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold deve essere compreso tra 0 e 1")
    if not 0.0 <= margin <= 1.0:
        raise ValueError("margin deve essere compreso tra 0 e 1")
    return RouterConfig(recent_messages, threshold, margin)


def load() -> RouterConfig:
    """Return one coherent live snapshot for the current routing decision."""
    global _CACHE_KEY, _CACHE
    key = (_signature(_REPO_PATH), _signature(_LIVE_PATH))
    if key == _CACHE_KEY:
        return _CACHE

    merged = _read(_REPO_PATH)
    merged.update(_read(_LIVE_PATH))
    try:
        config = _validated(merged)
    except ValueError as exc:
        LOG.warning("router config non valida (%s): uso i default", exc)
        config = RouterConfig()
    _CACHE_KEY = key
    _CACHE = config
    return config
