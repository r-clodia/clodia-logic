"""OpenTelemetry GenAI semantic conventions — the ONE place their names live
(clodia-platform#425 §2.1, #463).

The GenAI conventions (`gen_ai.*`) are still in *development* status: names
have changed between releases (`gen_ai.system` became `gen_ai.provider.name`)
and will change again. Every name the platform emits is therefore written here
and nowhere else; following a rename is an edit to this file.

Only METADATA is mapped. The conventions also define content attributes
(`gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`,
tool call arguments and results): the platform never emits them. Content
capture is off by construction, not by a flag — `CONTENT_ATTRIBUTES` lists
them so that a test can prove none of them is ever built.
"""
from __future__ import annotations

#: The version of the conventions these names were taken from.
SEMCONV_VERSION = "1.37.0"
SEMCONV_STATUS = "development"
SCHEMA_URL = f"https://opentelemetry.io/schemas/{SEMCONV_VERSION}"

# ── attribute names ──────────────────────────────────────────────────────────
OPERATION_NAME = "gen_ai.operation.name"
PROVIDER_NAME = "gen_ai.provider.name"
REQUEST_MODEL = "gen_ai.request.model"
RESPONSE_MODEL = "gen_ai.response.model"
USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
AGENT_NAME = "gen_ai.agent.name"
AGENT_ID = "gen_ai.agent.id"
CONVERSATION_ID = "gen_ai.conversation.id"
TOOL_NAME = "gen_ai.tool.name"
TOOL_CALL_ID = "gen_ai.tool.call.id"
ERROR_TYPE = "error.type"

#: Content-bearing attributes of the conventions. Never emitted (see above).
CONTENT_ATTRIBUTES = frozenset({
    "gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions",
    "gen_ai.tool.call.arguments", "gen_ai.tool.call.result", "gen_ai.prompt",
    "gen_ai.completion",
})

# ── operation names (`gen_ai.operation.name` values) ─────────────────────────
OP_INVOKE_AGENT = "invoke_agent"
OP_EXECUTE_TOOL = "execute_tool"
OP_CHAT = "chat"

#: OTLP span kinds (proto enum): the conventions give CLIENT for a call to a
#: remote agent or model, INTERNAL for an agent run in process.
SPAN_KIND_INTERNAL = 1
SPAN_KIND_CLIENT = 3

# ── Clodia's own attributes, next to the conventions ─────────────────────────
CLODIA_SPAWN = "clodia.spawn"
CLODIA_TOPIC_TIER = "clodia.topic.tier"
CLODIA_PROVIDER_ID = "clodia.provider.id"
CLODIA_PROVIDER_SEAL = "clodia.provider.seal"
CLODIA_RUNTIME = "clodia.runtime"
CLODIA_TURN_STATUS = "clodia.turn.status"

#: Clodia provider id → `gen_ai.provider.name` well-known value. A provider
#: that is not here is reported with its own id, which the conventions allow.
PROVIDERS = {
    "anthropic-api": "anthropic",
    "claude-pro-max": "anthropic",
    "claude-team": "anthropic",
    "aws-region-eu": "aws.bedrock",
    "openai-api": "openai",
    "codex": "openai",
    "scaleway": "scaleway",
}


def provider_name(provider_id: str | None) -> str | None:
    if not provider_id:
        return None
    return PROVIDERS.get(provider_id, provider_id)


def span_name(operation: str, target: str | None) -> str:
    """`{operation} {target}`, the naming rule of the conventions."""
    return f"{operation} {target}" if target else operation


def invoke_agent_attributes(*, seed: str | None, spawn: str | None,
                            conversation: str | None, model: dict | None,
                            status: str | None, error: str | None) -> dict:
    """The attributes of a turn's `invoke_agent` span. `model` is the block the
    audit trail records (`audit_events.model_block` + usage)."""
    m = model or {}
    usage = m.get("usage") or {}
    attrs = {
        OPERATION_NAME: OP_INVOKE_AGENT,
        AGENT_NAME: seed,
        AGENT_ID: spawn,
        CONVERSATION_ID: conversation,
        PROVIDER_NAME: provider_name(m.get("provider")),
        REQUEST_MODEL: m.get("request_name"),
        RESPONSE_MODEL: m.get("response_name"),
        USAGE_INPUT_TOKENS: usage.get("input_tokens"),
        USAGE_OUTPUT_TOKENS: usage.get("output_tokens"),
        ERROR_TYPE: error,
        CLODIA_SPAWN: spawn,
        CLODIA_TOPIC_TIER: m.get("topic_tier"),
        CLODIA_PROVIDER_ID: m.get("provider"),
        CLODIA_PROVIDER_SEAL: m.get("provider_seal"),
        CLODIA_RUNTIME: m.get("runtime"),
        CLODIA_TURN_STATUS: status,
    }
    return {k: v for k, v in attrs.items() if v is not None and v != ""}
