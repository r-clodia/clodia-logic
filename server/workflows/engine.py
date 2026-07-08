"""Motore dei workflow dichiarativi: esegue i run stage per stage.

Sostituto interno del vecchio flusso Trello (skill_consumer): lo stato vive
nella datadir (store.py), la vista è la board della webui, l'assegnazione è
per CAPABILITY (lane = skill richiesta): a ogni stage si sceglie un agente
che possiede la skill — preferendo gli specializzati, con i super come
fallback. Gate umani: dopo l'esecuzione di uno stage `human_gate` il run si
ferma in `waiting_approval` finché un umano non approva dalla board.

Il turno di stage è BLOCCANTE per il run (send_user_message) ma i run fra
loro sono concorrenti (semaforo). Un motore volutamente semplice: la
robustezza viene dallo stato persistito — un crash a metà stage lascia il
run in `running` con history parziale, e il tick lo riprende.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..agents.loader import registry
from ..agents.skill_sync import WILDCARDS, _all_skill_names, _pack_skill_names
from . import store

LOG = logging.getLogger("agent-server.workflows")

TICK_SECONDS = 20
_MAX_CONCURRENT_RUNS = 2
_STAGE_TIMEOUT = 10 * 60          # sotto il watchdog SDK: fallisce pulito, non appeso

_sem = asyncio.Semaphore(_MAX_CONCURRENT_RUNS)
_inflight: set[str] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _agent_skills(name: str) -> set[str]:
    try:
        spec = registry.get(name)
    except KeyError:
        return set()
    caps = list(getattr(spec, "capabilities", None) or [])
    if any(c in WILDCARDS for c in caps):
        return set(_all_skill_names())
    out: set[str] = set()
    for cap in caps:
        if cap.endswith("/*"):
            out.update(_pack_skill_names(cap[:-2]))
        else:
            out.add(cap)
    return out


def pick_agent(skill: str) -> str | None:
    """Agente che possiede la skill: prima gli specializzati (non-super,
    non-human), poi i super. Nessuno → None (gap riportato sul run)."""
    normals, supers = [], []
    for spec in registry.list():
        a_type = getattr(spec, "type", "normal")
        if a_type == "human":
            continue
        if skill in _agent_skills(spec.name):
            (supers if a_type == "super" else normals).append(spec.name)
    if normals:
        return sorted(normals)[0]
    if supers:
        return sorted(supers)[0]
    return None


def _stage_prompt(run: dict, stage: dict) -> str:
    lines = [
        f"[workflow · {run['plugin']}/{run['workflow']} · run {run['id']} · "
        f"lane «{stage['lane']}»]",
        f"Card: {run['title']}",
    ]
    if run.get("params"):
        lines.append(f"Parametri: {run['params']}")
    if run.get("topic"):
        t = run["topic"]
        lines.append(f"Pratica di riferimento: {t.get('tier')}/{t.get('name')} "
                     "(usa i verbi topic.* per leggere/scrivere lì).")
    done = [h for h in run["history"] if h.get("status") == "ok"]
    if done:
        lines.append("Output degli stage precedenti:")
        for h in done[-4:]:
            lines.append(f"- {h['lane']} ({h['agent']}): {h.get('summary', '')[:400]}")
    lines += [
        "",
        f"Esegui questo stage applicando la skill `{stage['skill']}` (segui il "
        "suo protocollo, inclusa la sezione Intake se richiede input che qui "
        "mancano — in quel caso NON inventare: descrivi cosa manca e termina "
        "con ESITO: BLOCCATO).",
        "Chiudi SEMPRE l'ultimo paragrafo con una riga:",
        "ESITO: OK | BLOCCATO | FALLITO — seguita da un riepilogo di 2-3 righe "
        "per lo stage successivo.",
    ]
    return "\n".join(lines)


def _parse_esito(reply: str) -> tuple[str, str]:
    """(status, summary) dalla riga ESITO: dell'agente. Default ok."""
    tail = (reply or "").strip()[-1200:]
    for line in reversed(tail.splitlines()):
        u = line.upper()
        if u.startswith("ESITO:"):
            if "FALLITO" in u:
                return "failed", tail
            if "BLOCCATO" in u:
                return "blocked", tail
            return "ok", tail
    return "ok", tail


async def _run_stage(run: dict) -> None:
    from ..sdk_runtime.session import ProviderNotConnected, manager

    idx = run["current"]
    stage = run["stages"][idx]
    agent = pick_agent(stage["skill"])
    if not agent:
        run["status"] = "failed"
        run["history"].append({
            "lane": stage["lane"], "skill": stage["skill"], "agent": None,
            "started_at": _now(), "finished_at": _now(), "status": "failed",
            "summary": f"nessun agente possiede la skill {stage['skill']} "
                       "(capability mancante nel roster dell'edizione)",
        })
        store.save_run(run)
        return

    entry = {"lane": stage["lane"], "skill": stage["skill"], "agent": agent,
             "started_at": _now(), "finished_at": None, "status": "running",
             "summary": ""}
    run["status"] = "running"
    run["history"].append(entry)
    store.save_run(run)

    chat_id = f"wf:{run['id']}:{idx}"
    try:
        try:
            chat = manager.get(chat_id)
        except KeyError:
            chat = await manager.create(chat_id=chat_id, kind=agent)
        chat.principal = "workflow"
        async with asyncio.timeout(_STAGE_TIMEOUT):
            reply = await chat.send_user_message(_stage_prompt(run, stage))
        status, summary = _parse_esito(reply)
    except ProviderNotConnected:
        status, summary = "failed", "provider non connesso per l'agente assegnato"
    except asyncio.TimeoutError:
        status, summary = "failed", f"timeout: nessun esito entro {_STAGE_TIMEOUT}s"
    except Exception as e:  # noqa: BLE001
        # Le cancellazioni del watchdog SDK arrivano con str() vuoto: cattura
        # almeno il tipo, così il fallimento è diagnosticabile dalla board.
        msg = str(e).strip() or f"{type(e).__name__} (turno interrotto dal watchdog?)"
        status, summary = "failed", f"errore stage: {msg}"

    entry["finished_at"] = _now()
    entry["status"] = status
    entry["summary"] = summary[-1500:]

    if status == "ok":
        if stage.get("human_gate"):
            run["status"] = "waiting_approval"
        elif idx + 1 < len(run["stages"]):
            run["current"] = idx + 1
            run["status"] = "pending"       # il tick eseguirà il prossimo stage
        else:
            run["status"] = "done"
    elif status == "blocked":
        run["status"] = "waiting_approval"  # un umano decide: riprova/annulla
    else:
        run["status"] = "failed"
    store.save_run(run)
    LOG.info("workflow %s stage %d (%s) → %s", run["id"], idx, stage["lane"], status)


def approve(run_id: str, by: str, note: str = "") -> dict:
    """Sblocca un run in waiting_approval: avanza al prossimo stage (o chiude)."""
    run = store.load_run(run_id)
    if not run:
        raise KeyError(run_id)
    if run["status"] != "waiting_approval":
        raise ValueError(f"run non in attesa di approvazione ({run['status']})")
    run["approvals"].append({"stage": run["current"], "by": by,
                             "verdict": "approved", "note": note, "at": _now()})
    if run["current"] + 1 < len(run["stages"]):
        run["current"] += 1
        run["status"] = "pending"
    else:
        run["status"] = "done"
    store.save_run(run)
    return run


def reject(run_id: str, by: str, note: str = "") -> dict:
    run = store.load_run(run_id)
    if not run:
        raise KeyError(run_id)
    if run["status"] != "waiting_approval":
        raise ValueError(f"run non in attesa di approvazione ({run['status']})")
    run["approvals"].append({"stage": run["current"], "by": by,
                             "verdict": "rejected", "note": note, "at": _now()})
    run["status"] = "rejected"
    store.save_run(run)
    return run


async def _guarded_run(run_id: str) -> None:
    async with _sem:
        run = store.load_run(run_id)
        if run and run["status"] == "pending":
            try:
                await _run_stage(run)
            except Exception as e:  # noqa: BLE001 — mai uccidere il tick
                LOG.exception("workflow %s: errore stage: %s", run_id, e)
    _inflight.discard(run_id)


async def tick_once() -> int:
    """Avvia gli stage dei run in stato pending. Ritorna quanti ne ha lanciati."""
    launched = 0
    for run in store.list_runs(include_done=False):
        if run["status"] == "pending" and run["id"] not in _inflight:
            _inflight.add(run["id"])
            asyncio.get_running_loop().create_task(_guarded_run(run["id"]))
            launched += 1
    return launched


async def engine_loop() -> None:
    LOG.info("workflow engine avviato (tick %ds)", TICK_SECONDS)
    while True:
        try:
            await tick_once()
        except Exception as e:  # noqa: BLE001
            LOG.warning("workflow tick fallito: %s", e)
        await asyncio.sleep(TICK_SECONDS)
