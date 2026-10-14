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
"""
from __future__ import annotations

import json
import os
from pathlib import Path

FILENAME = "channel-bindings.json"
LEGACY_FILENAME = "telegram-bindings.json"
LEGACY_CONNECTOR = "telegram"


def _data_dir() -> Path:
    return Path(os.environ.get("CLODIA_DATA", "/datadir"))


def _read(p: Path) -> dict:
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def load() -> dict:
    """`{connettore: {chat_id: {instance, tier, topic}}}`."""
    doc = _read(_data_dir() / FILENAME)
    conn = doc.get("connectors")
    if isinstance(conn, dict) and conn:
        return {c: dict(b or {}) for c, b in conn.items()}
    legacy = _read(_data_dir() / LEGACY_FILENAME)
    return {LEGACY_CONNECTOR: legacy} if legacy else {}


def for_connector(connector: str) -> dict:
    return dict(load().get(connector) or {})


def any_binding() -> bool:
    """C'è almeno una chat legata? È la domanda del loop di `main`: nessun
    binding ⇒ nessun trasporto da interrogare."""
    return any(b for b in load().values())
