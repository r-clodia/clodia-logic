#!/usr/bin/env python3
"""gmail-transport — stdio MCP envelope of google-pack's Gmail transport.

Only the protocol lives here: the tool, its schema and its behaviour are in
`gmail_transport_core.py`, which imports nothing beyond the stdlib — so the
transport can be tested in a suite that does not have the gateway's `mcp`.

Mounted by the gateway as backend `gmail-transport` (the plugin's `.mcp.json`)
and called ONLY by the gateway, for a Gmail account when the instance switch
is `connectors: {google: pack}`: the `email.*` verbs and every check stay in
the gateway, which hides this backend's `run` tool from agents.
clodia-platform#524. Same envelope as comms-pack's `mail_transport_mcp.py`.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gmail_transport_core as core  # noqa: E402

__version__ = core.__version__

app = Server("gmail-transport")

_TOOLS = [Tool(**spec) for spec in core.TOOL_SPECS]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return _TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    fn = core.DISPATCH.get(name)
    if fn is None:
        return [TextContent(type="text", text=f"ERROR: unknown tool: {name}")]
    try:
        # The client blocks (imaplib/smtplib) for up to `timeout` seconds:
        # in a thread, so the stdio loop keeps answering.
        result = await asyncio.to_thread(fn, arguments or {})
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    except Exception as e:  # noqa: BLE001
        # The message names the refused path or argument, never file contents.
        # Raised, not returned: the MCP server turns it into a result with
        # `isError`, so the gateway sees a failure and its cause (review of
        # clodia-logic#528).
        raise RuntimeError(f"{type(e).__name__}: {e}") from None


async def main():
    print(f"[gmail-transport-mcp v{__version__}] up", file=sys.stderr)
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
