"""Pack ops — consegna opzionale della riconciliazione all'agente sysadmin.

I pack dichiarano dipendenze (`requires:`) e datastore (`datastores:`) nel
manifest, curated dal pack developer; l'import le propaga in
`CLODIA_DATA/plugins/<nome>/plugin.yaml`.

Questo modulo non esegue provisioning direttamente: consegna un turno all'agente
`pack_ops.agent`, che usa i tool gateway dedicati (`packs.install_*`, `mcp.*`,
`rag.*`) e marca il setup completato quando la convergenza è verificata.

Trigger:
- post-import (packs.py): fire-and-forget dopo un import con dichiarazioni;
- boot reconcile (main.py, lifespan): se esistono dichiarazioni nei manifest.

Degradazione pulita: agente assente dal roster o provider non connesso →
nessun errore, si logga e i gap restano da chiudere a mano (report
post-install del builder).
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .. import instance_profile
from ..config import data_path

LOG = logging.getLogger("agent-server.pack_ops")

# Chat persistente della riconciliazione (una per agente): la storia dei run
# è il log operativo di Sysadmin, consultabile dalla webui.
_CHAT_PREFIX = "packops:"


def declarations() -> dict[str, dict]:
    """Manifest dei plugin con dichiarazioni pack ops.

    {plugin: {requires, datastores, rag_collections, mcp_servers}}. I server MCP
    fanno parte della riconciliazione quanto pip/npm/rag: un plugin importato da
    fonte non fidata NON viene montato automaticamente (`pack_mcp_mount`), quindi
    il mount resta un compito esplicito del reconciler.
    """
    found: dict[str, dict] = {}
    for manifest in sorted(Path(data_path("plugins")).glob("*/plugin.yaml")):
        try:
            meta = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(meta, dict):
            continue
        req, ds = meta.get("requires") or {}, meta.get("datastores") or []
        rc = meta.get("rag_collections") or []
        mcp = meta.get("mcp_servers") or {}
        if not isinstance(mcp, dict):
            mcp = {}
        if req or ds or rc or mcp:
            found[manifest.parent.name] = {"requires": req, "datastores": ds,
                                           "rag_collections": rc,
                                           "mcp_servers": mcp}
    return found


def _reconcile_prompt(reason: str, decls: dict[str, dict],
                      *, rag_enabled: bool = True) -> str:
    lines = [
        f"[piattaforma · pack ops · trigger: {reason}] Riconciliazione richiesta.",
        "",
        "Plugin con dichiarazioni (fonte di verità: i rispettivi "
        "$CLODIA_DATA/plugins/<nome>/plugin.yaml — rileggili tu stesso):",
    ]
    for name, d in decls.items():
        req = ", ".join(f"{k}:{v}" for k, v in (d.get("requires") or {}).items()) or "-"
        ds = ", ".join(x.get("path", "?") for x in (d.get("datastores") or [])) or "-"
        rc = ", ".join(
            f"{c.get('name')}({len(c.get('resources') or [])} risorse)"
            for c in (d.get("rag_collections") or [])) or "-"
        mcp = ", ".join(sorted(d.get("mcp_servers") or {})) or "-"
        lines.append(f"- {name} → requires [{req}] · datastores [{ds}] · "
                     f"rag_collections [{rc}] · mcp_servers [{mcp}]")
    lines += [
        "",
        "Esegui la convergenza con i tool dedicati del gateway, non con shell "
        "generica: packs.install_pip / packs.install_npm per `requires`, "
        "packs.check_command per `bin`/`system`, mcp.add + runtime.restart_agent "
        "per i server MCP dichiarati, rag.create_collection + rag.ingest per "
        "`rag_collections`.",
        "",
        "Per i mcp_servers: confronta i server dichiarati con `mcp.list`. Quelli "
        "assenti NON sono un guasto — l'auto-mount all'import è riservato alle "
        "fonti fidate, per le altre il mount è un atto esplicito. Montali con "
        "mcp.add passando la voce del manifest ({\"mcpServers\": {<nome>: "
        "{command, args, env}}}), poi runtime.restart_agent per gli agent che li "
        "usano. Se il mount va negato o fallisce, riportalo come gap: non "
        "marcare setup_done.",
    ]
    if rag_enabled:
        lines += [
            "",
            "Per le rag_collections: se una collection dichiarata non esiste ancora "
            "(rag.collections), PROVISIONALA con rag.create_collection e poi ingerisci "
            "le risorse iniziali via rag.ingest (collection + doc_name + version; il "
            "corpus/indice è infra pgvector, NON è nel pack). Le risorse con `url` vanno prima scaricate in "
            "un topic e poi ingerite; quelle con `path` sono file del pack. Idempotente: "
            "salta ciò che è già indicizzato. Se non hai gli strumenti per scaricare, "
            "riporta le risorse mancanti nel report per l'intervento umano.",
        ]
    elif any(d.get("rag_collections") for d in decls.values()):
        lines += [
            "",
            "ATTENZIONE — la feature `rag` è DISATTIVATA su questa istanza: i verbi "
            "rag.* non esistono (né in lista né al dispatch). Le rag_collections "
            "dichiarate qui sopra NON sono provisionabili: non cercare i tool e non "
            "considerarlo un tuo fallimento — riportale come gap di configurazione "
            "d'istanza (features.rag) e non marcare setup_done per quei pack.",
        ]
    lines += [
        "",
        "Verbi gated (packs.install_pip, packs.install_npm, mcp.add): ogni uso "
        "richiede l'approvazione umana in contesto. Se il trigger è automatico "
        "(boot/post-import) può non esserci nessuno all'ascolto: diniego o timeout "
        "NON vanno reinseguiti in loop. Registra il gap nel report — l'owner può "
        "sbloccare i run non presidiati con una delega permanente firmata sul "
        "singolo verbo, oppure approvare al primo turno interattivo.",
    ]
    return "\n".join(lines)


async def trigger_reconcile(reason: str) -> dict:
    """Consegna un turno di riconciliazione all'agente pack_ops (best-effort)."""
    decls = declarations()
    if not decls:
        return {"triggered": False, "reason": "nessuna dichiarazione nei plugin"}

    profile = instance_profile.load()
    cfg = profile.pack_ops
    if not cfg.enabled:
        LOG.info("pack ops: trigger %s saltato — profile.pack_ops.enabled=false", reason)
        return {"triggered": False, "reason": "pack_ops disabilitato dal profilo"}

    agent = cfg.agent
    # Import lazy: il runtime delle sessioni è pesante e questo modulo viene
    # importato anche in contesti che non lo usano (builder, test).
    from ..sdk_runtime.session import ProviderNotConnected, known_kind, manager

    if not known_kind(agent):
        LOG.info("pack ops: agente '%s' non nel roster — riconciliazione delegata "
                 "al report post-install (degradazione pulita)", agent)
        return {"triggered": False, "reason": f"agente '{agent}' non nel roster"}

    chat_id = f"{_CHAT_PREFIX}{agent}"
    try:
        try:
            chat = manager.get(chat_id)
        except KeyError:
            chat = await manager.create(chat_id=chat_id, kind=agent)
    except ProviderNotConnected:
        LOG.warning("pack ops: provider non connesso per '%s' — trigger saltato", agent)
        return {"triggered": False, "reason": "provider non connesso"}
    except Exception as e:  # noqa: BLE001
        LOG.warning("pack ops: creazione sessione fallita (%s)", str(e)[:120])
        return {"triggered": False, "reason": f"sessione: {str(e)[:120]}"}

    chat.principal = "platform"  # trigger di piattaforma, nessun principal umano
    rag_enabled = profile.features.rag != "off"
    await chat.send_user_message_async(
        _reconcile_prompt(reason, decls, rag_enabled=rag_enabled))
    LOG.info("pack ops: riconciliazione consegnata a '%s' (%s: %s)",
             agent, reason, ", ".join(decls))
    return {"triggered": True, "agent": agent, "plugins": sorted(decls)}
