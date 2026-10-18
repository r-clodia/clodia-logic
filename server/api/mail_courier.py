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


def envelopes(room: dict, skip: list[str], keys_only: bool = False) -> dict:
    r = requests.post(f"{_base_url()}/envelopes", headers=_headers(), timeout=_HTTP_TIMEOUT,
                      json={"tier": room["tier"], "name": room["name"],
                            "account": room["account"], "skip": list(skip),
                            "keys_only": bool(keys_only)})
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
    """One file per room+mailbox; the name is a hash of the three parts, so
    `a_b`+`c` and `a`+`b_c` never share a file (review of #549)."""
    import hashlib
    d = data_path("mail-courier-state")
    d.mkdir(parents=True, exist_ok=True)
    key = "\x00".join((room["tier"], room["name"], room["account"]))
    return d / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"


def _load(room: dict) -> dict:
    p = _state_path(room)
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
        return {"primed": bool(s.get("primed")), "keys": list(s.get("keys") or []),
                "fails": dict(s.get("fails") or {})}
    except (OSError, ValueError):
        return {"primed": False, "keys": [], "fails": {}}


def _save(room: dict, state: dict) -> None:
    state["keys"] = list(state.get("keys") or [])[-_KEYS_CAP:]
    state["fails"] = dict(list((state.get("fails") or {}).items())[-50:])
    _state_path(room).write_text(json.dumps(state), encoding="utf-8")


# ── delivery ──────────────────────────────────────────────────────────────

def _safe_filename(name: str) -> str:
    base = os.path.basename(str(name or "allegato")).replace("\\", "_").strip() or "allegato"
    keep = "".join(c if c.isalnum() or c in "._- " else "_" for c in base).strip()
    return (keep or "allegato")[:120]


#: Attempts at one envelope before it is posted without what keeps failing.
MAX_ATTEMPTS = 3


def _plain(value, limit: int = 300) -> str:
    """A sender-chosen header as one inert line: no line breaks (they would
    forge lines of the post), no `@` that mentions an agent of the room."""
    text = " ".join(str(value or "").split())
    return text.replace("@", "＠")[:limit]


def envelope_text(env: dict, saved: list[str], failed: list[str]) -> str:
    """The post: the envelope only. Never a body — the courier never has one."""
    righe = [f"📧 Nuova mail da {_plain(env.get('from')) or '?'}",
             f"Oggetto: {_plain(env.get('subject')) or '(senza oggetto)'}",
             f"Data: {_plain(env.get('date'), 80) or '?'}"]
    if env.get("to"):
        righe.append(f"A: {_plain(env['to'])}")
    if saved:
        righe.append("Allegati salvati: " + ", ".join(f"`{p}`" for p in saved))
    if failed:
        righe.append("Allegati non salvati: " + ", ".join(_plain(f, 120) for f in failed))
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
                base64.b64encode(raw).decode("ascii"), provenance="untrusted")
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
        out = await asyncio.to_thread(envelopes, room, state["keys"], not state["primed"])
    except requests.HTTPError as e:
        # 403: the room's rules refuse the mailbox now (list changed) — skip.
        LOG.info("mail courier: %s/%s skipped (%s)", room["tier"], room["name"],
                 getattr(e.response, "status_code", "?"))
        return 0
    if not state["primed"]:
        chiavi = [k for k in (out.get("keys") or []) if k]
        if not chiavi and out.get("envelopes"):        # a component without keys_only
            chiavi = [e["key"] for e in out["envelopes"] if e.get("key")]
        state["keys"].extend(chiavi)
        state["primed"] = True
        _save(room, state)
        LOG.info("mail courier: %s/%s primed with %d existing messages",
                 room["tier"], room["name"], len(chiavi))
        return 0
    nuove = list(out.get("envelopes") or [])
    consegnate = 0
    for env in reversed(nuove):           # oldest first
        key = env.get("key")
        try:
            await _deliver(room, env)
            consegnate += 1
        except Exception as e:  # noqa: BLE001 — retried at the next pass
            LOG.warning("mail courier: envelope for %s/%s not posted (%s)",
                        room["tier"], room["name"], type(e).__name__)
            fails = state.setdefault("fails", {})
            fails[key] = int(fails.get(key, 0)) + 1
            if fails[key] < MAX_ATTEMPTS:
                _save(room, state)
                # Later mail waits: delivery stays in arrival order.
                break
            # Last attempt: the envelope alone, naming what could not be saved.
            try:
                await topics_client.async_post_message(
                    room["tier"], room["name"], AUTHOR,
                    envelope_text(env, [], list(env.get("attachments") or [])),
                    kind="proxy")
                consegnate += 1
            except Exception:  # noqa: BLE001
                LOG.warning("mail courier: envelope for %s/%s given up after %d attempts",
                            room["tier"], room["name"], fails[key])
        state.get("fails", {}).pop(key, None)
        state["keys"].append(key)
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
