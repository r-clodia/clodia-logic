"""API proc-like degli spawn degli agent.

Modello "/spawns" (come /proc di Linux): ogni spawn vivo è una cartella
`clodia-data/spawns/<name>-<n>` che materializza seed+stato di un agent + uno
scratch. Questo endpoint la espone in sola lettura per la UI/observability.

Qui vive anche la vista **per-topic** degli spawn (issue clodia-platform#99),
che è una superficie diversa e molto più stretta: vedi `channel_spawns`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from ..agents.workspace import SPAWNS_ROOT
from ..sdk_runtime.session import manager
from . import topics_client
from .agents import _live_status
from .channels import _require_member, _split_ord

router = APIRouter()
LOG = logging.getLogger("agent-server.api.spawns")


@router.get("/api/spawns")
async def list_spawns() -> dict:
    """Elenca gli spawn attualmente materializzati sotto clodia-data/spawns/."""
    out: list[dict] = []
    if SPAWNS_ROOT.is_dir():
        for d in sorted(SPAWNS_ROOT.iterdir()):
            if not d.is_dir():
                continue
            # <name>-<instance> (instance tipicamente numerico, proc-like)
            agent, sep, instance = d.name.rpartition("-")
            if not sep:
                agent, instance = d.name, ""
            try:
                mtime = datetime.fromtimestamp(d.stat().st_mtime, timezone.utc).isoformat()
            except OSError:
                mtime = None
            out.append({
                "id": d.name,
                "agent": agent,
                "instance": instance,
                "has_scratch": (d / "scratch").is_dir(),
                "last_activity": mtime,
            })
    return {"spawns": out, "root": str(SPAWNS_ROOT)}


# --------------------------------------------------------------------------
# Albero degli spawn vivi di UN topic (issue clodia-platform#99)
# --------------------------------------------------------------------------
# Vocabolario CHIUSO degli stati esposti dall'albero. Nessuno stato NUOVO lato
# backend: è solo una proiezione di quelli che il runtime già produce
# (`_live_status` sopra `ClodiaStatus`) sui quattro colori chiesti dall'owner.
#
#   running    → 🟢  turno in corso
#   blocked    → 🟠  in "thinking" da oltre _STUCK_AFTER_S: fermo / in attesa
#   cancelling → 🟠  interruzione in corso: non lavora più, non è ancora idle
#   error      → 🔴  ClodiaStatus.ERROR sull'ultimo turno
#   idle       → ⚪  vivo, in attesa
#
# `stopped` non compare: la sessione non esiste più → il nodo esce dall'albero,
# senza tombstone (anche "è esistito" è metadato di presenza). Uno stato non
# previsto degrada a `unknown` (grigio): mai verde per default.
SPAWN_TREE_STATE = {
    "running": "running",
    "blocked": "blocked",
    "cancelling": "blocked",
    "error": "error",
    "idle": "idle",
}


@router.get("/clodia/channels/{tier}/{name}/spawns")
def channel_spawns(tier: str, name: str, request: Request) -> dict:
    """Spawn vivi **di questo topic**, per l'albero dei participant multi-spawn.

    Regola di esposizione: **presenza, non lavoro**. Esce di qui il nome
    dell'istanza (`fullstack-dev#2`) e lo stato del semaforo, nient'altro:
    niente output, prompt, argomenti, nomi di file, token, principal, runtime,
    timestamp o messaggio d'errore. Uno spawn può girare su un topic di classe
    superiore a quella di chi guarda l'albero: se il nodo raccontasse *cosa* sta
    facendo, l'albero diventerebbe un canale di leak trasversale fra classi
    (nota @security-engineer sulla #99). Lo stato `error` è un colore, non uno
    stack trace.

    Scoping: solo le sessioni del canale corrente, solo ai suoi membri
    (`_require_member`). Nessuna vista globale: presenza e conteggio degli
    spawn di un topic di classe superiore sono già metadato sensibile.
    """
    topic = topics_client.open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "canale non trovato")
    meta = topic.get("meta", {})
    _require_member(request, meta)
    prefix = f"chan:{tier}:{name}:"
    rows: list[dict] = []
    for chat in manager.list():
        cid = getattr(chat, "chat_id", "")
        if not cid.startswith(prefix):
            continue
        # Il responder è l'ULTIMO segmento del chat_id: un residuo con ':'
        # non è un'istanza di questo canale.
        label = cid[len(prefix):]
        if not label or ":" in label:
            continue
        try:
            d = chat.to_dict() or {}
        except Exception as e:  # noqa: BLE001 — una sessione malformata non rompe l'albero
            LOG.warning("spawn tree: sessione %s illeggibile: %s", cid, e)
            continue
        state = _live_status(d.get("status", ""), d.get("last_activity", ""))
        if state == "stopped":
            continue
        agent, ordinal = _split_ord(label)
        rows.append({
            "agent": agent,
            "instance": ordinal,
            "label": label,
            "state": SPAWN_TREE_STATE.get(state, "unknown"),
        })
    rows.sort(key=lambda r: ((r["agent"] or ""), r["instance"] or 0))
    return {"tier": meta.get("tier", tier), "name": name, "spawns": rows}
