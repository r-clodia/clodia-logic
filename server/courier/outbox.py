"""The courier's outbox (clodia-platform#736).

One JSON file per room under `courier-outbox/`, written atomically. An item:

    {id, tier, name, agent, to, subject, body, attachments, account,
     gate_key, state, at, updated, attempts, detail}

States: `pending_gate` (waiting for the room's decision on a new recipient),
`queued` (authorised, the courier sends it at its next pass), `sent`,
`failed`, `denied`, `expired`. Terminal items are kept (the last
`_KEEP_DONE` per room) so the room can see what happened.

The gateway queues (`/internal/courier/enqueue`); a gate decision moves
`pending_gate` items of that recipient and room (`decided`); the courier's
pass sends `queued` items and expires undecided ones after 24 hours.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from ..config import data_path

OUTBOX_DIR = data_path("courier-outbox")
#: An undecided message waits this long (owner, 25 Nov 2026).
PENDING_TTL_S = 24 * 3600
#: Sending attempts before a queued message is given up.
MAX_ATTEMPTS = 3
_KEEP_DONE = 200
STATES = ("pending_gate", "queued", "sent", "failed", "denied", "expired")
TERMINAL = ("sent", "failed", "denied", "expired")

_LOCK = threading.Lock()


def _path(tier: str, name: str) -> Path:
    h = hashlib.sha256(f"{tier}/{name}".encode()).hexdigest()[:24]
    return OUTBOX_DIR / f"{h}.json"


def _read(tier: str, name: str) -> list[dict]:
    try:
        data = json.loads(_path(tier, name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [i for i in (data.get("items") or []) if isinstance(i, dict)]


def _write(tier: str, name: str, items: list[dict]) -> None:
    live = [i for i in items if i.get("state") not in TERMINAL]
    done = [i for i in items if i.get("state") in TERMINAL][-_KEEP_DONE:]
    p = _path(tier, name)
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".outbox-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"tier": tier, "name": name, "items": done + live}, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def _rooms() -> list[tuple[str, str]]:
    out = []
    for f in sorted(OUTBOX_DIR.glob("*.json")) if OUTBOX_DIR.is_dir() else []:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("tier") and d.get("name"):
            out.append((str(d["tier"]), str(d["name"])))
    return out


def enqueue(item: dict) -> dict:
    """Add one message. The caller (the gateway) has decided `state`."""
    state = str(item.get("state") or "")
    if state not in ("pending_gate", "queued"):
        raise ValueError(f"stato iniziale non valido: {state!r}")
    tier, name = str(item.get("tier") or ""), str(item.get("name") or "")
    to = str(item.get("to") or "").strip().lower()
    if not (tier and name and "@" in to and str(item.get("body") or "").strip()):
        raise ValueError("tier, name, to e body richiesti")
    now = time.time()
    entry = {
        "id": "q" + secrets.token_hex(4),
        "tier": tier, "name": name, "agent": str(item.get("agent") or ""),
        "to": to, "subject": str(item.get("subject") or "")[:300],
        "body": str(item["body"]),
        "attachments": [str(x) for x in (item.get("attachments") or []) if str(x).strip()],
        "account": str(item.get("account") or ""),
        "gate_key": str(item.get("gate_key") or f"egress:email:mailto:{to}"),
        "state": state, "at": now, "updated": now, "attempts": 0, "detail": "",
    }
    with _LOCK:
        items = _read(tier, name)
        items.append(entry)
        _write(tier, name, items)
    return entry


def list_room(tier: str, name: str) -> list[dict]:
    with _LOCK:
        return _read(tier, name)


def _set(tier: str, name: str, ids: set[str], state: str, detail: str = "",
         attempt: bool = False) -> list[dict]:
    changed = []
    with _LOCK:
        items = _read(tier, name)
        for i in items:
            if i.get("id") in ids:
                i["state"], i["updated"] = state, time.time()
                if detail:
                    i["detail"] = detail[:300]
                if attempt:
                    i["attempts"] = int(i.get("attempts") or 0) + 1
                changed.append(dict(i))
        if changed:
            _write(tier, name, items)
    return changed


def decided(tier: str, name: str, gate_key: str, approved: bool) -> list[dict]:
    """A decision on the room's gate for a recipient: its waiting messages
    become `queued` (approved) or `denied`."""
    waiting = {i["id"] for i in list_room(tier, name)
               if i.get("state") == "pending_gate" and i.get("gate_key") == gate_key}
    return _set(tier, name, waiting, "queued" if approved else "denied",
                "" if approved else "destinatario negato")


def due(now: Optional[float] = None) -> tuple[list[dict], list[dict]]:
    """`(to_send, to_expire)` over every room: `queued` messages, and
    `pending_gate` ones older than `PENDING_TTL_S`."""
    now = time.time() if now is None else now
    send, expire = [], []
    for tier, name in _rooms():
        for i in list_room(tier, name):
            if i.get("state") == "queued":
                send.append(i)
            elif i.get("state") == "pending_gate" and now - float(i.get("at") or 0) > PENDING_TTL_S:
                expire.append(i)
    return send, expire


def mark(item: dict, state: str, detail: str = "", attempt: bool = False) -> dict | None:
    out = _set(item["tier"], item["name"], {item["id"]}, state, detail, attempt)
    return out[0] if out else None
