#!/usr/bin/env python3
"""google-transport — stdio MCP envelope of google-pack's Google API transport.

Only the protocol lives here: the tool, its schema and its behaviour are in
`google_transport_core.py`, which imports nothing beyond the stdlib — so the
transport can be tested in a suite that does not have the gateway's `mcp`.

Mounted by the gateway as backend `google-transport` (the plugin's `.mcp.json`)
and called ONLY by the gateway, when the instance switch is
`connectors: {google: pack}`: the 24 Google verbs, the perimeter and the
credentials stay in the gateway, which hides this backend's `request` tool
from agents. clodia-platform#524.
"""
from __future__ import annotations

import asyncio
import os
import sys

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import google_transport_core as core  # noqa: E402

__version__ = core.__version__

app = Server("google-transport")

_TOOLS = [Tool(**spec) for spec in core.TOOL_SPECS]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return _TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        # urllib blocks for up to the timeout: in a thread, so the stdio loop
        # keeps answering.
        text = await asyncio.to_thread(core.call, name, arguments or {})
    except core.ToolFailure as e:
        # Raised, not returned: the MCP server turns it into a result with
        # `isError`. `core.call` guarantees a generic message — never the
        # token, the arguments or the URL.
        raise RuntimeError(str(e)) from None
    return [TextContent(type="text", text=text)]


async def main():
    print(f"[google-transport-mcp v{__version__}] up", file=sys.stderr)
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
