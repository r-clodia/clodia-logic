"""Reading the channel bindings `instance ↔ chat` (read only).

The bindings are written by the gateway in the shared file
`<CLODIA_DATA>/channel-bindings.json`:

    { "version": 1,
      "connectors": { "<connector>": { "<chat_id>": {instance, tier, topic} } } }

The relay of the backend reads them (clodia-platform#521). If the file exists
but cannot be read (torn write, broken JSON, unexpected shape), the last
bindings read correctly in this process are kept (or none, if there are none
yet) and a warning is logged. The legacy `telegram-bindings.json` is no longer
read: Telegram was removed (clodia-platform#683).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

FILENAME = "channel-bindings.json"

LOG = logging.getLogger("agent-server.channel_bindings")

# Last bindings read correctly from the new file in this process: used when the
# file exists but cannot be read (e.g. the gateway is mid-write).
_last_good: dict = {}


def _data_dir() -> Path:
    return Path(os.environ.get("CLODIA_DATA", "/datadir"))


def _copy(bindings: dict) -> dict:
    return {c: dict(b) for c, b in bindings.items()}


def _parse_registry(p: Path) -> dict | None:
    """Bindings from the new file, or `None` if it exists but cannot be read."""
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    conn = doc.get("connectors", {})
    if not isinstance(conn, dict) or not all(
            isinstance(b, dict) or b is None for b in conn.values()):
        return None
    return {c: dict(b or {}) for c, b in conn.items()}


def load() -> dict:
    """`{connettore: {chat_id: {instance, tier, topic}}}`."""
    global _last_good
    p = _data_dir() / FILENAME
    if p.exists():
        parsed = _parse_registry(p)
        if parsed is None:
            LOG.warning("%s unreadable: keeping the last good bindings "
                        "(%d connectors)", p, len(_last_good))
            return _copy(_last_good)
        _last_good = parsed
        return _copy(parsed)
    return {}


def for_connector(connector: str) -> dict:
    return dict(load().get(connector) or {})


def any_binding() -> bool:
    """C'è almeno una chat legata? È la domanda del loop di `main`: nessun
    binding ⇒ nessun trasporto da interrogare."""
    return any(b for b in load().values())
