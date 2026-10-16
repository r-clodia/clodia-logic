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


def _plugin_owners(result: dict[str, Any]) -> list[tuple[str, str]]:
    """`[(plugin, pack che lo porta)]` — pack vuoto per un plugin sciolto.

    Il pack non è decorativo: è ciò che `register_mcp_core(..., pack=...)` scrive
    nella voce del backend, ed è l'unica prova con cui quel pack può poi usare
    quel namespace per i propri connettori (`backend_owners`, review di #354).
    Montare senza dirlo lasciava i backend dei pack «di nessuno», cioè riservati
    a tutti e rivendicabili dal primo che li rimonta (clodia-platform#539 §4).

    L'appartenenza si eredita scendendo: il nodo di un pack porta il proprio
    nome ai plugin che contiene.
    """
    out: list[tuple[str, str]] = []
    visti: set[str] = set()

    def add(name: Any, pack: str) -> None:
        n = str(name or "").strip()
        if n and n not in visti:
            visti.add(n)
            out.append((n, pack))

    def walk(node: Any, pack: str) -> None:
        if not isinstance(node, dict):
            return
        mio = str(node.get("pack") or "").strip() or pack
        add(node.get("plugin"), mio)
        for child in node.get("plugins") or []:
            walk(child, mio)
        for child in node.get("packs") or []:
            walk(child, mio)

    walk(result, "")
    return out


def _plugin_names(result: dict[str, Any]) -> list[str]:
    """I soli nomi, per chi non ha bisogno di sapere di chi sono."""
    return [p for p, _ in _plugin_owners(result)]


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
    for plugin, pack in _plugin_owners(result):
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
            # DI CHI è questo backend. Il gateway lo timbra nella voce e da
            # quel momento rifiuta che un altro lo sostituisca
            # (clodia-platform#539 §4): montare senza dirlo lasciava la
            # proprietà in bianco, e un backend senza proprietario è di chi
            # lo rimonta per primo.
            args["pack"] = pack
        status, data = gateway_pdp.gw_tool("mcp.add", args, principal)
        # The gateway's reply is not always a JSON object: a refused mount can
        # come back as plain text, and treating it as a dict turned a clear
        # refusal into a bare 500 for the whole update (clodia-platform#555).
        if not isinstance(data, dict):
            data = {"error": str(data)[:500]} if data else {}
        if status == 200:
            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            registered = result.get("registered")
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
