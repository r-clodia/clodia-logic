"""Smontaggio dei servizi di un pack/plugin rimosso — simmetrico a `pack_mcp_mount`.

L'installazione rende effettivi nel gateway i servizi dichiarati dal manifest;
la rimozione deve disfarli, altrimenti un pack disinstallato lascia dietro di sé
i suoi backend MCP montati e i grant dei suoi agenti: verbi ancora raggiungibili
per un pack che non c'è più, e nessuno che se ne accorga guardando la UI.

Le due direzioni non sono però speculari, e non devono esserlo:

- i **servizi** si smontano (`mcp.remove`), perché ricrearli è un `mcp.add`;
- i **dati** non si cancellano mai qui. Un datastore contiene roba dell'utente e
  una disinstallazione non è un consenso a distruggerla: i file vengono spostati
  fuori dalla directory del plugin (`plugin_import`) e la risposta dice dove
  sono finiti. Le collection RAG restano in piedi e vengono riportate come gap:
  archiviarle è un atto esplicito dell'admin, non un effetto collaterale.

Sta nel layer API, come il mount, perché è qui che c'è il principal autorizzato
dal PDP: il core importer resta filesystem-only.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

import yaml

from . import gateway_admin, gateway_pdp, pack_import, plugin_import

LOG = logging.getLogger("agent-server.api.pack_deprovision")


def _manifest(plugin: str) -> dict[str, Any]:
    path = plugin_import.PLUGINS_META_DIR / plugin / "plugin.yaml"
    if not path.is_file():
        return {}
    try:
        meta = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        LOG.warning("manifest di '%s' non leggibile per lo smontaggio: %s", plugin, e)
        return {}
    return meta if isinstance(meta, dict) else {}


def pack_plugins(pack: str) -> list[str]:
    """I plugin del pack secondo il suo manifest.

    Fallback sul nome del pack: un plugin importato sciolto vive come pack
    virtuale omonimo, senza `pack.yaml` — e i suoi servizi vanno smontati come
    quelli di ogni altro."""
    meta_path = pack_import.PACKS_META_DIR / pack / "pack.yaml"
    if not meta_path.is_file():
        return [pack]
    try:
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return [pack]
    names = [str(p) for p in (meta.get("plugins") or []) if str(p).strip()]
    return names or [pack]


def snapshot(plugin_names: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Servizi dichiarati dai plugin, letti PRIMA della rimozione.

    Dopo il `rmtree` i manifest non ci sono più: chiedere al filesystem cosa
    andava smontato quando è già stato cancellato è il modo in cui i backend
    sono rimasti montati fin qui."""
    out: dict[str, dict[str, Any]] = {}
    for plugin in plugin_names:
        meta = _manifest(plugin)
        servers = meta.get("mcp_servers")
        servers = sorted(servers) if isinstance(servers, dict) else []
        collections = [str(c.get("name")).strip()
                       for c in (meta.get("rag_collections") or [])
                       if isinstance(c, dict) and str(c.get("name") or "").strip()]
        if servers or collections:
            out[plugin] = {"mcp_servers": servers, "rag_collections": collections}
    return out


def snapshot_pack(pack: str) -> dict[str, dict[str, Any]]:
    return snapshot(pack_plugins(pack))


def _unmount(servers: list[str], principal: str) -> dict[str, Any]:
    unmounted: list[str] = []
    failed: list[dict[str, Any]] = []
    for server in servers:
        try:
            status, data = gateway_pdp.gw_tool("mcp.remove", {"name": server}, principal)
        except Exception as e:  # noqa: BLE001 — best-effort per disegno
            failed.append({"server": server, "status": 0, "error": str(e)[:200]})
            continue
        if status == 200:
            unmounted.append(server)
        else:
            failed.append({
                "server": server,
                "status": status,
                "error": str(data.get("detail") or data.get("error") or "unmount_failed")[:200],
            })
    return {"attempted": servers, "unmounted": unmounted, "failed": failed}


def _revoke_grants(agents: Iterable[str]) -> tuple[list[str], list[dict[str, Any]]]:
    """Azzera i verbi degli agenti del pack rimosso.

    SHORTCUT: revoca = upsert con `allowed_tools: []`, non cancellazione della
    voce. Regge perché un agente senza verbi non può fare nulla, ma lascia il
    nome nella config del gateway. La cancellazione vera richiede un DELETE su
    `/internal/agents/whitelist/<agent>` che in clodia-tools non esiste: quando
    ci sarà, questa funzione lo chiama e smette di scrivere liste vuote.
    """
    revoked: list[str] = []
    failed: list[dict[str, Any]] = []
    for agent in agents:
        try:
            gateway_admin.register_agent(agent, [])
            revoked.append(agent)
        except Exception as e:  # noqa: BLE001 — best-effort per disegno
            LOG.warning("revoca grant di '%s' fallita: %s", agent, str(e)[:120])
            failed.append({"agent": agent, "error": str(e)[:200]})
    return revoked, failed


def deprovision(snap: dict[str, dict[str, Any]], principal: str, *,
                agents: Iterable[str] = (),
                datastores_archived: Iterable[dict[str, Any]] = (),
                datastores_retained: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
    """Smonta i servizi del pack rimosso e riporta cosa è stato conservato.

    Best-effort con report: un gateway che non risponde non deve lasciare la
    rimozione a metà — i file sono già andati — ma il buco va detto, non
    ingoiato. Ritorna `{}` quando non c'era niente da smontare: una sezione
    vuota nella risposta è rumore che si impara a saltare.
    """
    report: dict[str, Any] = {}

    servers = sorted({s for d in snap.values() for s in d.get("mcp_servers") or []})
    if servers:
        report["mcp"] = _unmount(servers, principal)

    collections = sorted({c for d in snap.values() for c in d.get("rag_collections") or []})
    if collections:
        # Nessun verbo rag.* parte da qui: il corpus è infra condivisa e la sua
        # cancellazione non può essere l'effetto collaterale di un uninstall.
        report["rag_collections_kept"] = collections
        report["rag_note"] = ("collection RAG conservate: l'archiviazione è un "
                              "atto esplicito dell'admin (rag.*), non un effetto "
                              "della rimozione del pack.")

    archived = list(datastores_archived)
    if archived:
        report["datastores_archived"] = archived
    retained = list(datastores_retained)
    if retained:
        report["datastores_retained"] = retained

    agents = [a for a in agents if a]
    if agents:
        revoked, failed = _revoke_grants(agents)
        if revoked:
            report["grants_revoked"] = revoked
        if failed:
            report["grants_failed"] = failed

    return report


async def deprovision_async(snap: dict[str, dict[str, Any]], principal: str,
                            **kwargs) -> dict[str, Any]:
    """Come `deprovision`, fuori dall'event loop: i client del gateway sono
    `requests` sincroni e gli endpoint che li chiamano sono async."""
    return await asyncio.to_thread(deprovision, snap, principal, **kwargs)
