"""Pack ops — consegna della riconciliazione all'agente sysadmin (Saimon).

I pack dichiarano dipendenze (`requires:`) e datastore (`datastores:`) nel
manifest, curated dal pack developer; l'import le propaga in
`CLODIA_DATA/plugins/<nome>/plugin.yaml`. Questo modulo NON esegue nulla:
individua l'agente col ruolo `pack_ops.agent` del profilo (default `saimon`)
e gli consegna un turno di riconciliazione — è l'agente a convergere,
dentro il perimetro dichiarato (vedi il suo system-prompt).

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
# è il log operativo di Saimon, consultabile dalla webui.
_CHAT_PREFIX = "packops:"


def declarations() -> dict[str, dict]:
    """Manifest dei plugin con dichiarazioni pack ops: {plugin: {requires, datastores}}."""
    found: dict[str, dict] = {}
    for manifest in sorted(Path(data_path("plugins")).glob("*/plugin.yaml")):
        try:
            meta = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(meta, dict):
            continue
        req, ds = meta.get("requires") or {}, meta.get("datastores") or []
        if req or ds:
            found[manifest.parent.name] = {"requires": req, "datastores": ds}
    return found


def _reconcile_prompt(reason: str, decls: dict[str, dict]) -> str:
    lines = [
        f"[piattaforma · pack ops · trigger: {reason}] Riconciliazione richiesta.",
        "",
        "Plugin con dichiarazioni (fonte di verità: i rispettivi "
        "$CLODIA_DATA/plugins/<nome>/plugin.yaml — rileggili tu stesso):",
    ]
    for name, d in decls.items():
        req = ", ".join(f"{k}:{v}" for k, v in (d["requires"] or {}).items()) or "-"
        ds = ", ".join(x.get("path", "?") for x in d["datastores"]) or "-"
        lines.append(f"- {name} → requires [{req}] · datastores [{ds}]")
    lines += [
        "",
        "Applica il tuo protocollo di riconciliazione (idempotente, path "
        "persistenti in $CLODIA_DATA/runtime) e chiudi con il report.",
    ]
    return "\n".join(lines)


async def trigger_reconcile(reason: str) -> dict:
    """Consegna un turno di riconciliazione all'agente pack_ops (best-effort)."""
    decls = declarations()
    if not decls:
        return {"triggered": False, "reason": "nessuna dichiarazione nei plugin"}

    agent = instance_profile.load().pack_ops.agent
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
    await chat.send_user_message_async(_reconcile_prompt(reason, decls))
    LOG.info("pack ops: riconciliazione consegnata a '%s' (%s: %s)",
             agent, reason, ", ".join(decls))
    return {"triggered": True, "agent": agent, "plugins": sorted(decls)}
