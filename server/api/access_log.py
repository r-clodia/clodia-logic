"""Registro di ultimo accesso ai topic, per ordinare la lista Topics dal più
recentemente consultato al più vecchio.

Store JSON locale dell'agent-server: `{ "tier/name": "ISO8601-UTC" }`, timbrato
all'apertura o scrittura di un canale e letto da `list_topics`. Volutamente NEL
agent-server (non nel gateway): l'accesso è un fatto della UI servita da qui,
non del contenuto del topic, e così non si scrive sul meta del topic ad ogni
lettura.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from ..config import data_path

LOG = logging.getLogger("agent-server.api.access_log")

_STORE = data_path("agent-state") / "topic_access.json"
_LOCK = Lock()


def _key(tier: str, name: str) -> str:
    return f"{tier}/{name}"


def _load() -> dict:
    try:
        return json.loads(_STORE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def touch(tier: str, name: str) -> None:
    """Registra l'accesso al topic adesso (UTC ISO8601). Best-effort: un errore
    di scrittura non deve mai far fallire l'apertura/scrittura del canale."""
    try:
        with _LOCK:
            data = _load()
            data[_key(tier, name)] = datetime.now(timezone.utc).isoformat()
            _STORE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _STORE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_STORE)
    except Exception as e:  # noqa: BLE001
        LOG.warning("topic_access touch fallito per %s/%s: %s", tier, name, e)


def all_times() -> dict:
    """Mappa `{'tier/name': iso}` di tutti gli accessi noti."""
    with _LOCK:
        return _load()


def get(tier: str, name: str) -> str | None:
    return all_times().get(_key(tier, name))
