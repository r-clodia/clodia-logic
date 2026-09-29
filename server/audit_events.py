"""Events only the agent-server knows, deposited on the gateway's audit trail
(clodia-platform#433, #434, #435, #442, #443).

The trail is written and signed by the gateway (clodia-tools `server/audit/`),
on a volume this process does not mount. What the gateway cannot observe —
that a turn started, why, on which provider and model, how it ended — is
reported here through `POST /internal/audit/event`, authenticated by the
orchestrator secret, and recorded there with `actor.source = "agent-server"`.

`turn.start` also opens the spawn's trace on the gateway: every verb the
spawn calls until `turn.end` carries the same W3C trace id.

Reporting never changes the turn: a gateway that does not answer costs a log
line, not a failed turn.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Any

LOG = logging.getLogger("agent-server.audit")

_TIMEOUT = float(os.environ.get("CLODIA_AUDIT_REPORT_TIMEOUT", "3"))


def new_trace_id() -> str:
    """W3C trace-id: 16 random bytes, hex, never all zero."""
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def _url() -> str:
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/")
    base = mcp.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return base + "/internal/audit/event"


def _prune(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {k: _prune(v) for k, v in obj.items()}
        return {k: v for k, v in out.items() if v not in (None, {}, [])}
    return obj


async def report(event: dict) -> bool:
    """Send one event. True if the gateway recorded it."""
    secret = (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip()
    if not secret:
        return False  # no gateway trust channel (dev): nothing to report to
    try:
        import httpx
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(_url(), json=_prune(event),
                             headers={"X-Orchestrator-Secret": secret})
        if r.status_code != 200:
            LOG.warning("audit: %s not recorded by the gateway (%s)",
                        event.get("type"), r.status_code)
            return False
        return True
    except Exception as e:  # noqa: BLE001 - the turn must not depend on it
        LOG.warning("audit: %s not reported (%s)", event.get("type"), type(e).__name__)
        return False


# ── the turn ──────────────────────────────────────────────────────────────────
def _scope(tier: str | None, name: str | None) -> dict | None:
    return {"tier": tier, "topic": name} if tier and name else None


def _seed_and_spawn(label: str) -> dict:
    seed = label.rsplit("-", 1)[0] if label.rsplit("-", 1)[-1].isdigit() else label
    return {"seed": seed, "spawn": label}


class Turn:
    """One turn of one spawn, from start to end, on the trail."""

    def __init__(self, *, tier: str | None, name: str | None, label: str, chat_id: str | None,
                 principal: str | None, trigger: dict | None):
        self.trace_id, self.span_id = new_trace_id(), new_span_id()
        self.tier, self.name, self.label, self.chat_id = tier, name, label, chat_id
        self.principal, self.trigger = principal, dict(trigger or {})
        self._t0 = time.monotonic()

    def _base(self, etype: str, action: str) -> dict:
        return {"type": etype, "action": action, "resource": self.chat_id,
                "trace_id": self.trace_id, "span_id": self.span_id,
                "scope": _scope(self.tier, self.name),
                "agent": _seed_and_spawn(self.label),
                "actor": {"type": "agent", "id": self.label, "on_behalf": self.principal}}

    async def start(self) -> None:
        ev = self._base("turn.start", "start")
        # Why this turn exists (#442): what woke it, who asked, through which
        # chain. The trigger event id links back to the message or job.
        ev["decision"] = {"trigger": self.trigger or None}
        await report(ev)

    async def end(self, *, status: str, error: str | None = None,
                  model: dict | None = None, usage: dict | None = None) -> None:
        ev = self._base("turn.end", status)
        ev["result"] = {"status": status, "error": error,
                        "duration_ms": int((time.monotonic() - self._t0) * 1000)}
        if model or usage:
            ev["model"] = {**(model or {}), "usage": usage or None}
        await report(ev)


def outcome_of_reply(reply: str | None) -> tuple[str, str | None]:
    """A runtime returns a NOTE, not an exception, for an interrupted turn."""
    text = reply or ""
    if text.startswith("⏱ Turno interrotto dal watchdog"):
        return "watchdog", "watchdog_kill"
    if text.startswith("⏹ Inferenza interrotta"):
        return "interrupted", "user_interrupt"
    return "ok", None


def failure_class(e: BaseException) -> str:
    """The cause of a failed turn, as a class (never the message: it can carry data)."""
    msg = str(e).lower()
    if "non convergente" in msg or "entro" in msg and "turno" in msg:
        return "turn_timeout"
    if isinstance(e, TimeoutError) or "timeout" in msg:
        return "timeout"
    if "session not started" in msg or "sessione" in msg and "terminat" in msg:
        return "session_dead"
    if "provider" in msg:
        return "provider"
    return type(e).__name__


def model_block(chat, tier: str | None) -> dict:
    """Provider, region, SEAL and model of the turn that just ran (#434, #435).

    Recorded, not inferred: the provider the SESSION was created with
    (`session_provider`), not the one the platform would pick now, and the
    model the API RETURNED next to the one the agent declared.
    """
    from .sdk_runtime import session as S
    from .api import providers as P
    kind = getattr(chat, "kind", None)
    override = getattr(chat, "_runtime_override", None)
    pid = S.session_provider(chat) or S._runtime_provider(kind, override)
    try:
        env = P.provider_extra_env(pid) or {}
    except Exception:  # noqa: BLE001
        env = {}
    try:
        declared = S._declared_model(kind, override)
    except Exception:  # noqa: BLE001
        declared = None
    seal = P.provider_seal(pid)
    return {
        "provider": pid,
        "provider_region": env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
        or env.get("SCW_DEFAULT_REGION"),
        "provider_seal": seal,
        "topic_tier": tier,
        "seal_ok": P.provider_meets_tier(pid, tier) if tier else None,
        "request_name": declared,
        "response_name": getattr(chat, "_last_response_model", None),
        "runtime": getattr(chat, "agent_sdk", None) or type(chat).__name__,
    }
