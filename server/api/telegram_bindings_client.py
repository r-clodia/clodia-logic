"""Lettura dei binding Telegram `messaggero-#N ↔ chat_id` (sola lettura).

I binding sono scritti dal gateway (verbi telegram.listen/unlisten) nel file
condiviso `<CLODIA_DATA>/telegram-bindings.json`. Il relay del backend li legge.
Schema: { "<chat_id>": { "instance", "tier", "topic" } }.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _path() -> Path:
    base = os.environ.get("CLODIA_DATA", "/datadir")
    return Path(base) / "telegram-bindings.json"


def load() -> dict:
    p = _path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return {}
