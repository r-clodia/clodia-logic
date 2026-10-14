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


def _plugin_entries(result: dict[str, Any]) -> list[tuple[str, str | None]]:
    """`(plugin, pack)` dei plugin appena importati, nell'ordine in cui
    compaiono.

    Il PACK si porta dietro perché è ciò che prova la proprietà del backend
    montato (clodia-platform#539 §4, seguito di #354): `mcp.add` lo scrive
    nella voce del backend, e da lì i connettori di quel pack possono usarne il
    namespace. Si ricava dal CONTENITORE — un plugin sta sempre dentro un pack,
    anche quando il pack è il wrapper omonimo generato per un plugin nudo — non
    dal plugin, che non sa chi lo contiene. `None` quando il contenitore manca:
    meglio un mount senza proprietario (com'era prima) che uno timbrato con un
    nome indovinato.
    """
    out: list[tuple[str, str | None]] = []
    visti: set[str] = set()

    def add(name: Any, pack: str | None) -> None:
        n = str(name or "").strip()
        if n and n not in visti:
            visti.add(n)
            out.append((n, pack))

    def walk(node: Any, pack: str | None) -> None:
        if not isinstance(node, dict):
            return
        mio = str(node.get("pack") or "").strip() or pack
        add(node.get("plugin"), mio)
        for child in node.get("plugins") or []:
            walk(child, mio)
        for child in node.get("packs") or []:
            walk(child, mio)

    walk(result, None)
    return out


def _plugin_names(result: dict[str, Any]) -> list[str]:
    """Solo i nomi, per chi non ha bisogno di sapere a quale pack appartengono
    (`packs.py`, che legge le dichiarazioni per plugin)."""
    return [plugin for plugin, _pack in _plugin_entries(result)]


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


def auto_mount_imported_mcp(result: dict[str, Any], principal: str,
                            *, trusted: bool = False) -> dict[str, Any]:
    """Monta via gateway i server MCP dichiarati dai plugin appena importati.

    Mutazione in-place di `result`: aggiunge `mcp_mount` solo se c'erano server
    MCP da tentare. I fallimenti sono riportati come warning strutturati invece
    di essere silenziati.

    Prima Legge / supply-chain: montare un MCP server = avviare il processo/URL
    che il manifest dichiara (`command`/`args`). Per le fonti NON fidate (import
    di pack/plugin da zip o URL arbitrari) NON si monta automaticamente — sarebbe
    esecuzione di codice di terzi al solo import: i server sono segnalati come
    `pending` e l'owner li monta esplicitamente dalla sezione Tools (dopo la
    review del security-engineer). Il mount automatico è riservato alle fonti
    `trusted` = update di un pack FIRST-PARTY dal proprio upstream (codice nostro).
    """
    mounted: list[str] = []
    failed: list[dict[str, Any]] = []
    attempted: list[str] = []
    pending: list[str] = []
    for plugin, pack in _plugin_entries(result):
        servers = _manifest_mcp(plugin)
        if not servers:
            continue
        attempted.extend(k for k in sorted(servers) if k not in attempted)
        if not trusted:
            # fonte non fidata → NON montare, solo segnalare (barriera umana).
            pending.extend(k for k in sorted(servers) if k not in pending)
            continue
        args: dict[str, Any] = {"config": {"mcpServers": servers}}
        if pack:
            # Chi monta è il pack, e il gateway lo registra nella voce del
            # backend: è l'unica prova con cui quel pack può usarne il
            # namespace per i propri connettori (#539 §4). Senza, il mount
            # restava «di nessuno» e il connettore del pack veniva rifiutato
            # come se reclamasse il namespace di un altro.
            args["pack"] = pack
        status, data = gateway_pdp.gw_tool("mcp.add", args, principal)
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
        entry: dict[str, Any] = {
            "attempted": attempted,
            "mounted": mounted,
            "failed": failed,
        }
        if pending:
            entry["pending"] = pending
            entry["note"] = ("mount NON automatico per pack/plugin importati da "
                             "fonte esterna: montali dalla sezione Tools dopo la "
                             "review (Prima Legge: un import non avvia processi).")
        result["mcp_mount"] = entry
    return result


def mounted_backends(principal: str) -> list[str] | None:
    """I backend MCP montati DAVVERO, secondo `runtime.mcp_servers`.

    `None` (non `[]`) quando il gateway non risponde: la lista vuota direbbe
    «non c'è montato niente» e farebbe apparire in drift ogni pack installato.
    """
    try:
        status, data = gateway_pdp.gw_tool("runtime.mcp_servers", {}, principal)
    except Exception as e:  # noqa: BLE001
        LOG.warning("lettura backend montati non disponibile: %s", str(e)[:120])
        return None
    if status != 200:
        return None
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    backends = result.get("mcp_backends") or []
    if not isinstance(backends, list):
        return None
    names = {str(b.get("name") or "").strip() if isinstance(b, dict) else str(b).strip()
             for b in backends}
    return sorted(n for n in names if n)
