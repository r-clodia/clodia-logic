"""Optional OpenTelemetry export — off by default, content capture always off
(clodia-platform#425 §2.1, #463).

OTel is correlation, not evidence (#425 §2.2): the audit trail stays the
record, and nothing here is needed for it to be complete. What this adds, when
an owner switches it on, is the turn as an OTel trace whose id IS the audit
trace id of the turn:

- the agent-server exports one `invoke_agent <seed>` span per turn, with the
  turn's trace id and span id, and the GenAI attributes of `otel_genai`;
- the runtimes export their own spans, wired here so that they carry the
  turn's trace wherever the runtime can take it (see RUNTIMES below).

Configuration (environment of the agent-server):

    CLODIA_OTEL_EXPORT=1                         switch (default off)
    CLODIA_OTEL_ENDPOINT=http://otel-collector:4318   OTLP/HTTP base URL

The endpoint must be reachable from the agent container, which only sees the
internal network: run the collector there and add its name to
`CLODIA_NO_PROXY_EXTRA`, or the runtimes' exporters go to the egress proxy.

## What each runtime can carry (measured on the pinned versions)

- **claude** (Claude Code CLI through the Agent SDK): exports spans with
  `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` + `OTEL_TRACES_EXPORTER=otlp`, and
  parents them under `TRACEPARENT` — read from the environment **when the
  process starts**. The session's CLI process lives across turns (and
  re-opening it loses the conversation), so its spans carry a trace id per
  SPAWN, not per turn. It cannot carry the turn's trace id; the per-message
  `trace_context` it also reads is honoured only in its remote mode. The join
  is recorded instead: every `turn.start` links the runtime's trace, and the
  runtime's MCP calls carry its `traceparent` to the gateway, which links it
  on the `tool.call` (clodia-tools 2.74.0).
- **codex** (`codex exec`, one process per turn): reads `TRACEPARENT` at start
  and exports spans with an `[otel] trace_exporter` in `config.toml`. It is
  started with the TURN's traceparent: its spans carry the turn's trace id.
  `log_user_prompt = false`; the log and metrics exporters are set to `none`
  (codex's default metrics exporter would otherwise be Statsig).
- **opencode** (`opencode serve`, long-lived): exports only when
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set, and then exports its LOGS too, with no
  switch to keep them apart; its AI SDK spans (`experimental.openTelemetry`)
  record prompts and outputs by default with no option to turn that off. Both
  would break "content capture off": NOT wired. It reads no inbound
  `TRACEPARENT` either.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Mapping

from . import otel_genai as G

LOG = logging.getLogger("agent-server.otel")

#: Runtime switches that turn CONTENT capture on. Removed from every spawn's
#: environment, whether export is on or not: they must not be inherited from
#: the container by accident.
CONTENT_FLAGS = (
    "OTEL_LOG_USER_PROMPTS", "OTEL_LOG_ASSISTANT_RESPONSES", "OTEL_LOG_TOOL_DETAILS",
    "OTEL_LOG_TOOL_CONTENT", "OTEL_LOG_RAW_API_BODIES", "OTEL_LOG_MANAGED_SETTINGS",
    "ENABLE_BETA_TRACING_DETAILED", "BETA_TRACING_ENDPOINT",
    "CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH",
)

#: Which runtimes are wired, and how (see the module docstring).
RUNTIMES = {"claude": "spawn-trace", "codex": "turn-trace", "opencode": "not-wired"}


def enabled() -> bool:
    on = (os.environ.get("CLODIA_OTEL_EXPORT") or "").strip().lower() in ("1", "true", "yes", "on")
    return on and bool(endpoint())


def endpoint() -> str | None:
    v = (os.environ.get("CLODIA_OTEL_ENDPOINT") or "").strip().rstrip("/")
    return v or None


def traces_url() -> str | None:
    base = endpoint()
    return f"{base}/v1/traces" if base else None


def traceparent(trace_id: str, span_id: str) -> str:
    return f"00-{trace_id}-{span_id}-01"


def strip_content_flags(env: dict) -> dict:
    for k in CONTENT_FLAGS:
        env.pop(k, None)
    return env


def _resource_attrs(seed: str | None, spawn: str | None) -> str:
    from urllib.parse import quote
    pairs = [("service.namespace", "clodia"), ("clodia.agent", seed), (G.CLODIA_SPAWN, spawn)]
    return ",".join(f"{k}={quote(str(v), safe='')}" for k, v in pairs if v)


# ── claude ────────────────────────────────────────────────────────────────────
def claude_env(*, seed: str | None, spawn: str | None) -> tuple[dict, str | None]:
    """`(env, runtime_trace_id)` for a Claude Code CLI process, or `({}, None)`
    when export is off. Traces only; metrics and logs exporters `none`."""
    if not enabled():
        return {}, None
    runtime_trace = secrets.token_hex(16)
    env = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": traces_url(),
        "OTEL_RESOURCE_ATTRIBUTES": _resource_attrs(seed, spawn),
        "TRACEPARENT": traceparent(runtime_trace, secrets.token_hex(8)),
    }
    return env, runtime_trace


# ── codex ─────────────────────────────────────────────────────────────────────
def codex_config_toml() -> str:
    """The `[otel]` block of codex's `config.toml`; empty when export is off."""
    if not enabled():
        return ""
    return ("\n[otel]\n"
            "log_user_prompt = false\n"
            'exporter = "none"\n'
            'metrics_exporter = "none"\n'
            f'trace_exporter = {{ otlp-http = {{ endpoint = "{traces_url()}", protocol = "json" }} }}\n')


def codex_turn_env(trace_id: str | None, span_id: str | None) -> dict:
    """`TRACEPARENT` of the turn for one `codex exec`, when export is on."""
    if not (enabled() and trace_id and span_id):
        return {}
    return {"TRACEPARENT": traceparent(trace_id, span_id)}


# ── the agent-server's own span ───────────────────────────────────────────────
def _any_value(v) -> dict:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def _attrs(d: Mapping) -> list:
    return [{"key": k, "value": _any_value(v)} for k, v in d.items()]


def turn_span_payload(*, trace_id: str, span_id: str, parent_span_id: str | None,
                      name: str, start_ns: int, end_ns: int, attributes: dict,
                      error: bool, links: list | None = None) -> dict:
    """One OTLP/HTTP JSON `ExportTraceServiceRequest` with the turn's span."""
    from . import __version__ as _v  # noqa: PLC0415
    span = {
        "traceId": trace_id, "spanId": span_id, "name": name,
        "kind": G.SPAN_KIND_INTERNAL,
        "startTimeUnixNano": str(start_ns), "endTimeUnixNano": str(end_ns),
        "attributes": _attrs(attributes),
        "status": {"code": 2 if error else 1},
    }
    if parent_span_id:
        span["parentSpanId"] = parent_span_id
    if links:
        span["links"] = [{"traceId": ln["trace_id"],
                          **({"spanId": ln["span_id"]} if ln.get("span_id") else {}),
                          "attributes": _attrs(ln.get("attributes") or {})} for ln in links]
    return {"resourceSpans": [{
        "resource": {"attributes": _attrs({"service.name": "clodia-agent-server",
                                           "service.namespace": "clodia",
                                           "service.version": _v})},
        "scopeSpans": [{"scope": {"name": "clodia.agent-server", "version": _v},
                        "schemaUrl": G.SCHEMA_URL, "spans": [span]}],
    }]}


async def export(payload: dict) -> bool:
    url = traces_url()
    if not url:
        return False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.post(url, json=payload)
        return r.status_code < 300
    except Exception as e:  # noqa: BLE001 - observability never breaks a turn
        LOG.debug("otel: span not exported (%s)", type(e).__name__)
        return False


def now_ns() -> int:
    return time.time_ns()
