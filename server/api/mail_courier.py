"""The deterministic mail courier (clodia-platform#531).

Moves inbound mail into the topics whose room has a mailbox in its OWN
`inbox:` list — code, never a model, and never the message body:

- asks the gateway which rooms have a mailbox (`/internal/mail/courier/rooms`);
- for each, the envelopes it has not delivered yet
  (`/internal/mail/courier/envelopes`): sender, recipients, subject, date,
  attachment names. The gateway runs comms-pack's component inside the room's
  claims, so the room's `inbox:` list and, on a strict room, its declared
  senders (#503) apply before anything leaves the gateway;
- stores each attachment in the topic's files as `untrusted`, byte-identical
  (`/internal/mail/courier/attachment` → `topics_client.put_file`);
- posts the envelope into the topic under the connector's identity (`posta`,
  kind `proxy`), naming the saved files. Agents that work on the topic read
  the message, if they need to, with `email.read` under the usual taint rules.

The first pass over a mailbox only records what is there (no flood of old
mail into the topic): delivery starts with what arrives afterwards.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os

import requests

from ..colony import pki
from ..config import data_path
from . import topics_client

LOG = logging.getLogger("agent-server.mail_courier")

_PRINCIPAL = os.environ.get("CLODIA_PROVIDER_PRINCIPAL", "clodia")
_TOKEN_TTL = 300
_HTTP_TIMEOUT = 60
#: The author of the courier's posts: the connector, not an agent.
AUTHOR = "posta"
#: Keys remembered per mailbox and room.
_KEYS_CAP = 500


def _base_url() -> str:
    explicit = os.environ.get("CLODIA_TOOLS_MAIL_COURIER_URL")
    if explicit:
        return explicit.rstrip("/")
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/")
    base = mcp.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return f"{base}/internal/mail/courier"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {pki.mint_session_token(_PRINCIPAL, ttl_seconds=_TOKEN_TTL)}"}


# ── the gateway ───────────────────────────────────────────────────────────

def rooms() -> list[dict]:
    r = requests.get(f"{_base_url()}/rooms", headers=_headers(), timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    return list(r.json().get("rooms") or [])


def envelopes(room: dict, skip: list[str]) -> dict:
    r = requests.post(f"{_base_url()}/envelopes", headers=_headers(), timeout=_HTTP_TIMEOUT,
                      json={"tier": room["tier"], "name": room["name"],
                            "account": room["account"], "skip": list(skip)})
    r.raise_for_status()
    return r.json()


def attachment(room: dict, email_id: str, filename: str) -> dict:
    r = requests.post(f"{_base_url()}/attachment", headers=_headers(), timeout=_HTTP_TIMEOUT,
                      json={"tier": room["tier"], "name": room["name"],
                            "account": room["account"], "email_id": str(email_id),
                            "filename": filename})
    r.raise_for_status()
    return r.json()


# ── state ─────────────────────────────────────────────────────────────────

def _state_path(room: dict):
    d = data_path("mail-courier-state")
    d.mkdir(parents=True, exist_ok=True)
    key = f"{room['tier']}_{room['name']}_{room['account']}"
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in key)
    return d / f"{safe}.json"


def _load(room: dict) -> dict:
    p = _state_path(room)
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
        return {"primed": bool(s.get("primed")), "keys": list(s.get("keys") or [])}
    except (OSError, ValueError):
        return {"primed": False, "keys": []}


def _save(room: dict, state: dict) -> None:
    state["keys"] = list(state.get("keys") or [])[-_KEYS_CAP:]
    _state_path(room).write_text(json.dumps(state), encoding="utf-8")


# ── delivery ──────────────────────────────────────────────────────────────

def _safe_filename(name: str) -> str:
    base = os.path.basename(str(name or "allegato")).replace("\\", "_").strip() or "allegato"
    keep = "".join(c if c.isalnum() or c in "._- " else "_" for c in base).strip()
    return (keep or "allegato")[:120]


def envelope_text(env: dict, saved: list[str], failed: list[str]) -> str:
    """The post: the envelope only. Never a body — the courier never has one."""
    righe = [f"📧 Nuova mail da {env.get('from') or '?'}",
             f"Oggetto: {env.get('subject') or '(senza oggetto)'}",
             f"Data: {env.get('date') or '?'}"]
    if env.get("to"):
        righe.append(f"A: {env['to']}")
    if saved:
        righe.append("Allegati salvati: " + ", ".join(f"`{p}`" for p in saved))
    if failed:
        righe.append("Allegati non salvati: " + ", ".join(failed))
    righe.append(f"(id {env.get('id')} nella casella; il testo si legge con email.read)")
    return "\n".join(righe)


async def _deliver(room: dict, env: dict) -> None:
    saved, failed = [], []
    for name in env.get("attachments") or []:
        try:
            got = await asyncio.to_thread(attachment, room, env["id"], name)
            raw = base64.b64decode(got["content_b64"])
            fname = f"posta-{str(env.get('key') or '')[:8]}-{_safe_filename(got.get('filename') or name)}"
            await topics_client.async_put_file(
                room["tier"], room["name"], fname,
                base64.b64encode(raw).decode("ascii"))
            saved.append(f"files/{fname}")
        except Exception as e:  # noqa: BLE001 — one attachment never stops the envelope
            LOG.warning("mail courier: attachment of %s/%s not saved (%s)",
                        room["tier"], room["name"], type(e).__name__)
            failed.append(str(name))
    await topics_client.async_post_message(room["tier"], room["name"], AUTHOR,
                                           envelope_text(env, saved, failed), kind="proxy")


async def run_room(room: dict) -> int:
    """One pass over one room's mailbox. Returns how many envelopes were
    delivered."""
    state = _load(room)
    try:
        out = await asyncio.to_thread(envelopes, room, state["keys"])
    except requests.HTTPError as e:
        # 403: the room's rules refuse the mailbox now (list changed) — skip.
        LOG.info("mail courier: %s/%s skipped (%s)", room["tier"], room["name"],
                 getattr(e.response, "status_code", "?"))
        return 0
    nuove = list(out.get("envelopes") or [])
    if not state["primed"]:
        state["keys"].extend(e["key"] for e in nuove if e.get("key"))
        state["primed"] = True
        _save(room, state)
        LOG.info("mail courier: %s/%s primed with %d existing messages",
                 room["tier"], room["name"], len(nuove))
        return 0
    consegnate = 0
    for env in reversed(nuove):           # oldest first
        try:
            await _deliver(room, env)
            consegnate += 1
        except Exception as e:  # noqa: BLE001 — retried at the next pass
            LOG.warning("mail courier: envelope for %s/%s not posted (%s)",
                        room["tier"], room["name"], type(e).__name__)
            continue
        state["keys"].append(env["key"])
        _save(room, state)
    return consegnate


async def run_cycle() -> int:
    total = 0
    try:
        stanze = await asyncio.to_thread(rooms)
    except Exception as e:  # noqa: BLE001
        LOG.warning("mail courier: rooms unavailable (%s)", type(e).__name__)
        return 0
    for room in stanze:
        try:
            total += await run_room(room)
        except Exception as e:  # noqa: BLE001 — one room never stops the others
            LOG.warning("mail courier: %s/%s failed (%s)", room.get("tier"),
                        room.get("name"), type(e).__name__)
    return total
