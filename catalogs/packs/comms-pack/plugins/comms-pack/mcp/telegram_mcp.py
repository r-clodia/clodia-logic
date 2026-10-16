#!/usr/bin/env python3
"""telegram — involucro MCP stdio del connettore Telegram di comms-pack.

Qui c'è solo il trasporto: i nomi, gli schemi e il comportamento stanno in
`telegram_core.py`, che non importa niente oltre la stdlib. La separazione non è
estetica — è ciò che permette di provare il connettore (la tabella dei verbi
compresa) in una suite che non ha la stessa versione di `mcp` del gateway, cioè
senza che il controllo dipenda da un pacchetto installato altrove.

Montato dal gateway come backend `telegram` (`.mcp.json` del plugin); i verbi
tornano agli agenti namespaced `telegram.<tool>` attraverso il proxy, quindi
dopo whitelist, deny, gate, egress e taint. clodia-platform#522.
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

import telegram_core as core  # noqa: E402

__version__ = core.__version__

app = Server("telegram")

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
        # Le chiamate a Telegram sono sincrone e bloccanti (urllib): in un
        # thread, se no `updates` in long-poll fermerebbe l'intero backend.
        result = await asyncio.to_thread(fn, arguments or {})
        return [TextContent(type="text",
                            text=json.dumps(result, indent=2, ensure_ascii=False))]
    except Exception as e:  # noqa: BLE001
        return [TextContent(type="text", text=f"ERROR: {type(e).__name__}: {e}")]


async def main():
    print(f"[telegram-mcp v{__version__}] up", file=sys.stderr)
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
