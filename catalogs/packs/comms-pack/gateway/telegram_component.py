"""comms-pack's gateway component: Telegram, run inside the gateway.

clodia-platform#522/#558. Until comms-pack 2.0.x Telegram was this pack's MCP
backend `telegram` (a stdio process the gateway started per call, with the bot
token in its environment). From 2.1.0 the same code (`telegram_core.py`) runs
here, and on `connectors: {telegram: pack}`:

- the agent verbs `telegram.inbox`, `.lease_acquire`, `.poll`, `.send`,
  `.send_file`, `.lease_release`, `.roster`, `.status` are served from here,
  through the gateway's normal `call_tool` path (whitelist, deny, gates, the
  egress PDP on this pack's `telegram` connector, taint, audit). `agent` and
  `channel` come from the call's signed claims (`host.agent()`,
  `host.channel()`), `chat_id` and a topic file's bytes are filled by the
  gateway from the manifest (`defaults:`, `files:`), as before;
- the relay's two internal operations — the long-poll of updates and the
  download of an attachment — are SERVICES (`telegram_updates`,
  `telegram_download`): the gateway calls them, no agent can;
- the bot token stays in the gateway's vault (`telegram_bot_token`), read
  through `host.credentials` only while the switch hands Telegram to this pack;
- the state (offset, per-chat queues, leases) is this component's private file
  on the gateway's volume (`host.state_path`).

On `native` the component stays pinned on standby and the gateway's own
Telegram code serves.
"""
from __future__ import annotations

from typing import Any

from mcp.types import Tool

from . import telegram_core as core

#: The verbs an agent may call; `updates` and `download` are the relay's.
AGENT_VERBS = ("inbox", "lease_acquire", "poll", "send", "send_file",
               "lease_release", "roster", "status")

CREDENTIAL = "telegram_bot_token"


def _handler(host: Any, fn):
    def handle(arguments: dict) -> dict:
        a = dict(arguments or {})
        # Identity and room from the SIGNED claims of this call — never what
        # the caller wrote (the manifest's `context:` fills them too).
        a["agent"] = host.agent() or ""
        a["channel"] = host.channel() or ""
        return fn(a)
    return handle


#: How long the bundle read from the vault is reused (a disconnect or a new
#: token takes effect within this): every read is an audit
#: record, and the relay long-polls every ~25 s.
_BUNDLE_TTL_S = 60.0


def register(host: Any) -> None:
    import time

    creds = host.credentials(CREDENTIAL)
    cache: dict = {}

    def bundle() -> dict:
        now = time.monotonic()
        if cache.get("until", 0.0) > now:
            return cache["bundle"]
        b = creds.read_internal(CREDENTIAL) or {}
        cache.update(bundle=b, until=now + _BUNDLE_TTL_S)
        return b

    def token() -> str:
        return str(bundle().get("token") or "")

    def bot_username() -> str:
        return str(bundle().get("bot_username") or "")

    core.TOKEN_SOURCE = token
    core.BOT_USERNAME_SOURCE = bot_username
    core.STATE_FILE = host.state_path("telegram-state.json")
    specs = {s["name"]: s for s in core.TOOL_SPECS}
    for name in AGENT_VERBS:
        spec = specs[name]
        host.register_verb(
            Tool(name=f"telegram.{name}", description=spec["description"],
                 inputSchema=spec["inputSchema"]),
            _handler(host, core.DISPATCH[name]))
    host.register_service("telegram_updates", lambda arguments: core.updates(dict(arguments or {})))
    host.register_service("telegram_download", lambda arguments: core.download(dict(arguments or {})))
