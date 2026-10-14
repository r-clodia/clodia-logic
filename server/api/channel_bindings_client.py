"""Lettura dei binding di canale `istanza ↔ chat` (sola lettura).

I binding sono scritti dal gateway (verbi `*.listen`/`*.unlisten`, rotta
`…/telegram-link`) nel file condiviso `<CLODIA_DATA>/channel-bindings.json`:

    { "version": 1,
      "connectors": { "telegram": { "<chat_id>": {instance, tier, topic} } } }

Il relay del backend li legge (clodia-platform#521). Sostituisce
`telegram_bindings_client`, che leggeva lo stesso dato quando il trasporto
poteva essere uno solo.

**Fallback sul file storico** `telegram-bindings.json`: i due componenti si
deployano separatamente, e per qualche minuto l'agent-server nuovo convive col
gateway vecchio. Senza fallback, in quella finestra, ogni chat legata smette di
essere servita — in silenzio e proprio dove il silenzio non si nota, perché un
messaggio che non arriva non lascia traccia. La lettura è solo lettura: a
migrare è il gateway, che di quel file è il proprietario.

The fallback applies **only when the new file does not exist**. If it exists
it is the source of truth, even when it lists no chat: after the migration the
legacy file is a stale copy, and reading it again would bring back the chats
the owner unbound. If it exists but cannot be read (torn write, broken JSON,
unexpected shape), the last bindings read correctly in this process are kept
(or none, if there are none yet) and a warning is logged; the legacy file is
never used in that case.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

FILENAME = "channel-bindings.json"
LEGACY_FILENAME = "telegram-bindings.json"
LEGACY_CONNECTOR = "telegram"

LOG = logging.getLogger("agent-server.channel_bindings")

# Last bindings read correctly from the new file in this process: used when the
# file exists but cannot be read (e.g. the gateway is mid-write).
_last_good: dict = {}


def _data_dir() -> Path:
    return Path(os.environ.get("CLODIA_DATA", "/datadir"))


def _read(p: Path) -> dict:
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return {}


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
                        "(%d connectors), not the legacy file", p, len(_last_good))
            return _copy(_last_good)
        _last_good = parsed
        return _copy(parsed)
    legacy = _read(_data_dir() / LEGACY_FILENAME)
    return {LEGACY_CONNECTOR: legacy} if legacy else {}


def for_connector(connector: str) -> dict:
    return dict(load().get(connector) or {})


def any_binding() -> bool:
    """C'è almeno una chat legata? È la domanda del loop di `main`: nessun
    binding ⇒ nessun trasporto da interrogare."""
    return any(b for b in load().values())
