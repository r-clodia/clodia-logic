"""Auto-mount dei server MCP dichiarati dai plugin importati.

Il core importer resta filesystem-only; il layer API, gia' autorizzato dal PDP,
usa questo helper per rendere effettivi sul gateway i backend MCP dichiarati.
"""
from __future__ import annotations

import logging
from typing import Any

import yaml

from . import gateway_pdp, plugin_import

LOG = logging.getLogger("agent-server.api.pack_mcp_mount")


def _plugin_names(result: dict[str, Any]) -> list[str]:
    names: list[str] = []

    def add(name: Any) -> None:
        n = str(name or "").strip()
        if n and n not in names:
            names.append(n)

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        add(node.get("plugin"))
        for child in node.get("plugins") or []:
            walk(child)
        for child in node.get("packs") or []:
            walk(child)

    walk(result)
    return names


def _manifest_mcp(plugin: str) -> dict[str, Any]:
    manifest = plugin_import.PLUGINS_META_DIR / plugin / "plugin.yaml"
    if not manifest.is_file():
        return {}
    try:
        meta = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        LOG.warning("plugin manifest %s non leggibile per mount MCP: %s", plugin, e)
        return {}
    servers = meta.get("mcp_servers") or {}
    return servers if isinstance(servers, dict) else {}


def auto_mount_imported_mcp(result: dict[str, Any], principal: str) -> dict[str, Any]:
    """Monta via gateway i server MCP dichiarati dai plugin appena importati.

    Mutazione in-place di `result`: aggiunge `mcp_mount` solo se c'erano server
    MCP da tentare. I fallimenti sono riportati come warning strutturati invece
    di essere silenziati.
    """
    mounted: list[str] = []
    failed: list[dict[str, Any]] = []
    attempted: list[str] = []
    for plugin in _plugin_names(result):
        servers = _manifest_mcp(plugin)
        if not servers:
            continue
        attempted.extend(k for k in sorted(servers) if k not in attempted)
        status, data = gateway_pdp.gw_tool(
            "mcp.add",
            {"config": {"mcpServers": servers}},
            principal,
        )
        if status == 200:
            registered = data.get("result", {}).get("registered")
            if isinstance(registered, list):
                mounted.extend(str(x) for x in registered if str(x) not in mounted)
            else:
                mounted.extend(k for k in sorted(servers) if k not in mounted)
        else:
            failed.append({
                "plugin": plugin,
                "servers": sorted(servers),
                "status": status,
                "error": data.get("detail") or data.get("error") or "mount_failed",
            })
    if attempted:
        result["mcp_mount"] = {
            "attempted": attempted,
            "mounted": mounted,
            "failed": failed,
        }
    return result
