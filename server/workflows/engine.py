"""Motore dei workflow CONVERSAZIONALI: un run è un topic effimero.

Ogni stadio è un turno dell'agente assegnato NEL topic del run (non più una
sessione isolata `wf:*`). L'interazione — avvio, gate, sblocco — avviene in
chat con intervista + choice pills, riusando `run_topic_turn` con responder
forzato = agente dello stadio.

Stati del run:
  pending  → un tick eseguirà (o ri-eseguirà) il turno dello stadio corrente
  running  → un turno è in volo
  await    → l'agente ha CHIESTO qualcosa (intake) o è a un GATE: si attende
             una risposta nel topic (da umano O da un altro agente). No timeout.
  done | failed | cancelled → terminali

Segnale di fine stadio (regola confermata): l'ultimo messaggio dell'agente
contiene `ESITO: OK` → avanza; `ESITO: FALLITO` → fallisce; nessun `ESITO:`
(o BLOCCATO) → l'agente sta chiedendo → `await`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..agents.loader import registry
from ..agents.skill_sync import WILDCARDS, _all_skill_names, _pack_skill_names
from . import store

LOG = logging.getLogger("agent-server.workflows")

TICK_SECONDS = 15
_MAX_CONCURRENT_RUNS = 2
_STAGE_TIMEOUT = 10 * 60          # cap del singolo TURNO (l'await fra turni non scade)

_sem = asyncio.Semaphore(_MAX_CONCURRENT_RUNS)
_inflight: set[str] = set()

_GATE_CHOICES = "Approva,Rimanda con modifiche,Annulla"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── assegnazione per capability ──────────────────────────────────────────────
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
    """Agente che possiede la skill: prima gli specializzati, poi i super."""
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


# ── topic effimero del run ───────────────────────────────────────────────────
def ensure_topic(run: dict) -> dict:
    """Crea (idempotente) il topic effimero del run: participants = agenti degli
    stadi + l'umano che avvia. Tier dal workflow (default SEAL-1). Best-effort:
    se fallisce, il run resta senza topic e il tick lo segnala."""
    if run.get("topic"):
        return run
    from ..api import topics_client

    tier = run.get("tier") or "SEAL-1"
    agents: list[str] = []
    for st in run["stages"]:
        a = pick_agent(st["skill"])
        if a and a not in agents:
            agents.append(a)
    owner = run.get("requested_by") or "clodia"
    participants = list(dict.fromkeys([owner, *agents]))
    name = f"wf-{run['id']}"
    meta = {
        "title": f"▶ {run['plugin']}/{run['workflow']} — {run['title']}",
        "type": "workflow",
        "owner": owner,
        "participants": participants,
        "contact_agent": agents[0] if agents else "clodia",
        "run_class": "workflow",     # marca la classe: sezione archivio dedicata
        "parent_run": run["id"],
    }
    try:
        topics_client.create_topic(tier, name, meta)
        run["topic"] = {"tier": tier, "name": name}
        store.save_run(run)
        LOG.info("workflow %s: topic effimero %s/%s creato (%d participants)",
                 run["id"], tier, name, len(participants))
    except Exception as e:  # noqa: BLE001
        LOG.warning("workflow %s: creazione topic fallita: %s", run["id"], str(e)[:160])
    return run


# ── prompt e parsing ─────────────────────────────────────────────────────────
def _stage_kickoff(run: dict, stage: dict) -> str:
    """Istruzione di avvio stadio, postata come messaggio system nel topic."""
    lines = [f"[workflow · stadio «{stage['lane']}»]"]
    done = [h for h in run["history"] if h.get("status") == "ok"]
    if done:
        lines.append("Esito degli stadi precedenti (dettagli nei messaggi sopra):")
        for h in done[-4:]:
            lines.append(f"- {h['lane']}: {(h.get('summary') or '')[:300]}")
    lines += [
        "",
        f"Applica la skill `{stage['skill']}` seguendo il suo protocollo. Se ti "
        "mancano input (sezione Intake), NON inventare: chiedili in chat (usa "
        "il marcatore <!-- choices=A,B,C --> quando le opzioni sono enumerabili) "
        "e ATTENDI la risposta — non scrivere ESITO finché non hai tutto.",
        "Quando lo stadio è completo, chiudi con una riga "
        "`ESITO: OK` (o `ESITO: FALLITO`) + un riepilogo di 2-3 righe.",
    ]
    return "\n".join(lines)


def _parse_esito(reply: str) -> tuple[str, str]:
    """(status, summary). status ∈ ok | failed | asked. 'asked' = nessun ESITO
    o BLOCCATO → l'agente sta chiedendo qualcosa (→ await). Robusto: trova
    l'ULTIMA occorrenza di `ESITO:` ovunque nel testo (non solo a inizio riga)
    e legge l'esito che segue."""
    tail = (reply or "").strip()[-1600:]
    up = tail.upper()
    pos = up.rfind("ESITO:")
    if pos == -1:
        return "asked", tail              # nessun ESITO → sta chiedendo
    after = up[pos + len("ESITO:"): pos + len("ESITO:") + 40]
    if "FALLITO" in after:
        return "failed", tail
    if "OK" in after:
        return "ok", tail
    return "asked", tail                  # ESITO: BLOCCATO → sta chiedendo


def _msg_count(run: dict) -> int:
    from ..api import topics_client
    t = run["topic"]
    try:
        return len(topics_client.list_messages(t["tier"], t["name"], limit=500))
    except Exception:  # noqa: BLE001
        return 0


def _last_message(run: dict) -> dict | None:
    from ..api import topics_client
    t = run["topic"]
    try:
        msgs = topics_client.list_messages(t["tier"], t["name"], limit=1)
        return msgs[-1] if msgs else None
    except Exception:  # noqa: BLE001
        return None


# ── esecuzione di un turno di stadio ─────────────────────────────────────────
async def _run_stage_turn(run: dict) -> None:
    """Esegue UN turno dell'agente dello stadio corrente nel topic del run e
    aggiorna lo stato in base all'ESITO."""
    from .. import api  # noqa: F401
    from ..api import channels, topics_client
    from ..sdk_runtime.session import ProviderNotConnected

    if not run.get("topic"):
        ensure_topic(run)
    if not run.get("topic"):
        run["status"] = "failed"
        run["history"].append({"stage_idx": run["current"], "lane": run["stages"][run["current"]]["lane"],
                               "skill": run["stages"][run["current"]]["skill"], "agent": None,
                               "started_at": _now(), "finished_at": _now(), "status": "failed",
                               "summary": "impossibile creare il topic del run"})
        store.save_run(run)
        return

    idx = run["current"]
    stage = run["stages"][idx]
    agent = pick_agent(stage["skill"])
    if not agent:
        run["status"] = "failed"
        run["history"].append({
            "stage_idx": idx, "lane": stage["lane"], "skill": stage["skill"], "agent": None,
            "started_at": _now(), "finished_at": _now(), "status": "failed",
            "summary": f"nessun agente possiede la skill {stage['skill']}"})
        store.save_run(run)
        return

    t = run["topic"]
    last = run["history"][-1] if run["history"] else None
    continuation = bool(last and last.get("stage_idx") == idx and last.get("status") in ("running", "await"))
    if continuation:
        entry = last
    else:
        entry = {"stage_idx": idx, "lane": stage["lane"], "skill": stage["skill"], "agent": agent,
                 "started_at": _now(), "finished_at": None, "status": "running", "summary": ""}
        run["history"].append(entry)
    run["status"] = "running"
    store.save_run(run)

    # Prima esecuzione dello stadio: semina l'istruzione come messaggio system.
    if not continuation:
        try:
            topics_client.post_message(t["tier"], t["name"], "workflow",
                                       _stage_kickoff(run, stage), kind="system")
        except Exception as e:  # noqa: BLE001
            LOG.warning("workflow %s: kickoff non postato: %s", run["id"], str(e)[:120])

    try:
        async with asyncio.timeout(_STAGE_TIMEOUT):
            responder, reply = await channels.run_topic_turn(
                t["tier"], t["name"], {"tier": t["tier"]},
                trigger_text="", principal_hint="workflow", responder_hint=agent)
        if responder is None:
            status, summary = "failed", f"responder '{agent}' non idoneo al tier {t['tier']}"
        else:
            status, summary = _parse_esito(reply)
    except ProviderNotConnected:
        status, summary = "failed", "provider non connesso per l'agente assegnato"
    except asyncio.TimeoutError:
        status, summary = "failed", f"timeout: nessun esito entro {_STAGE_TIMEOUT}s"
    except Exception as e:  # noqa: BLE001
        msg = str(e).strip() or f"{type(e).__name__} (watchdog?)"
        status, summary = "failed", f"errore turno: {msg}"

    entry["summary"] = (summary or "")[-1500:]

    if status == "ok":
        entry["finished_at"] = _now()
        entry["status"] = "ok"
        if stage.get("human_gate"):
            # Gate conversazionale: posta il go/no-go come pill e attendi.
            try:
                topics_client.post_message(
                    t["tier"], t["name"], agent,
                    f"Stadio «{stage['lane']}» completato. Procedo?\n"
                    f"<!-- choices={_GATE_CHOICES} -->", kind="ai")
            except Exception:  # noqa: BLE001
                pass
            run["status"] = "await"
            run["gate_pending"] = True
            run["await_marker"] = _msg_count(run)
        elif idx + 1 < len(run["stages"]):
            run["current"] = idx + 1
            run["status"] = "pending"
        else:
            run["status"] = "done"
    elif status == "asked":
        # l'agente ha chiesto qualcosa → await intake (nessun timeout)
        entry["status"] = "await"
        run["status"] = "await"
        run["gate_pending"] = False
        run["await_marker"] = _msg_count(run)
    else:
        entry["finished_at"] = _now()
        entry["status"] = "failed"
        run["status"] = "failed"

    store.save_run(run)
    if run["status"] in ("done", "failed", "cancelled"):
        _finalize(run)
    LOG.info("workflow %s stadio %d (%s) → %s [%s]",
             run["id"], idx, stage["lane"], status, run["status"])


# ── risoluzione dell'await (nuovo messaggio nel topic) ───────────────────────
def _has_new_reply(run: dict) -> bool:
    """True se nel topic è arrivato un messaggio DOPO l'ingresso in await, da
    qualcuno diverso dall'agente dello stadio (umano o altro agente)."""
    if not run.get("topic"):
        return False
    marker = run.get("await_marker") or 0
    if _msg_count(run) <= marker:
        return False
    last = _last_message(run)
    if not last:
        return False
    idx = run["current"]
    stage_agent = pick_agent(run["stages"][idx]["skill"])
    # il messaggio che sblocca non è dell'agente stesso e non è il kickoff system
    author = last.get("author")
    return author not in (stage_agent, "workflow")


async def _resolve_await(run: dict) -> None:
    """Un messaggio è arrivato mentre il run era in await: risolve gate o
    riprende l'intervista."""
    idx = run["current"]
    if run.get("gate_pending"):
        last = _last_message(run) or {}
        text = (last.get("text") or "").strip().lower()
        by = last.get("author") or "?"
        if text.startswith("approva"):
            run["approvals"].append({"stage": idx, "by": by, "verdict": "approved",
                                     "note": "", "at": _now()})
            run["gate_pending"] = False
            run["await_marker"] = None
            if idx + 1 < len(run["stages"]):
                run["current"] = idx + 1
                run["status"] = "pending"
            else:
                run["status"] = "done"
        elif text.startswith("annulla"):
            run["approvals"].append({"stage": idx, "by": by, "verdict": "cancelled",
                                     "note": "", "at": _now()})
            run["gate_pending"] = False
            run["await_marker"] = None
            run["status"] = "cancelled"
        else:
            # "Rimanda con modifiche" o testo libero → rifà lo stadio con la nota
            run["approvals"].append({"stage": idx, "by": by, "verdict": "rimanda",
                                     "note": last.get("text", "")[:500], "at": _now()})
            run["gate_pending"] = False
            run["await_marker"] = None
            run["status"] = "pending"   # non-continuation → nuovo kickoff dello stadio
    else:
        # intake: c'è la risposta → riprendi lo stadio (continuazione)
        run["await_marker"] = None
        run["status"] = "pending"
    store.save_run(run)
    if run["status"] in ("done", "cancelled"):
        _finalize(run)


# ── controllo (board: scorciatoie) ───────────────────────────────────────────
def approve(run_id: str, by: str, note: str = "") -> dict:
    """Scorciatoia board: approva il gate corrente (= pill Approva nel topic)."""
    run = store.load_run(run_id)
    if not run:
        raise KeyError(run_id)
    if not (run["status"] == "await" and run.get("gate_pending")):
        raise ValueError(f"run non a un gate ({run['status']})")
    idx = run["current"]
    run["approvals"].append({"stage": idx, "by": by, "verdict": "approved",
                             "note": note, "at": _now()})
    run["gate_pending"] = False
    run["await_marker"] = None
    if idx + 1 < len(run["stages"]):
        run["current"] = idx + 1
        run["status"] = "pending"
    else:
        run["status"] = "done"
        _finalize(run)
    store.save_run(run)
    return run


def reject(run_id: str, by: str, note: str = "") -> dict:
    """Scorciatoia board: respingi il gate (= pill Rimanda) → rifà lo stadio."""
    run = store.load_run(run_id)
    if not run:
        raise KeyError(run_id)
    if not (run["status"] == "await" and run.get("gate_pending")):
        raise ValueError(f"run non a un gate ({run['status']})")
    run["approvals"].append({"stage": run["current"], "by": by, "verdict": "rimanda",
                             "note": note, "at": _now()})
    run["gate_pending"] = False
    run["await_marker"] = None
    run["status"] = "pending"
    store.save_run(run)
    return run


async def cancel(run_id: str, by: str, note: str = "") -> dict:
    """Interrompe un run non terminale → cancelled; archivia il topic."""
    run = store.load_run(run_id)
    if not run:
        raise KeyError(run_id)
    if run["status"] in ("done", "failed", "cancelled"):
        raise ValueError(f"run già terminato ({run['status']})")
    _inflight.add(run_id)
    if run["history"] and run["history"][-1].get("status") in ("running", "await"):
        run["history"][-1]["status"] = "cancelled"
        run["history"][-1]["summary"] = f"interrotto da {by}" + (f": {note}" if note else "")
    run["approvals"].append({"stage": run["current"], "by": by, "verdict": "cancelled",
                             "note": note, "at": _now()})
    run["gate_pending"] = False
    run["status"] = "cancelled"
    store.save_run(run)
    _finalize(run)
    LOG.info("workflow %s cancellato da %s", run_id, by)
    return run


def _finalize(run: dict) -> None:
    """A terminale: archivia il topic del run (sezione archivio dedicata)."""
    if not run.get("topic"):
        return
    from ..api import topics_client
    t = run["topic"]
    try:
        topics_client.post_message(t["tier"], t["name"], "workflow",
                                   f"Run terminato: {run['status']}.", kind="system")
        topics_client.archive_topic(t["tier"], t["name"])
    except Exception as e:  # noqa: BLE001
        LOG.warning("workflow %s: archiviazione topic fallita: %s", run["id"], str(e)[:120])


# ── loop ─────────────────────────────────────────────────────────────────────
async def _guarded_stage(run_id: str) -> None:
    async with _sem:
        run = store.load_run(run_id)
        if run and run["status"] == "pending":
            try:
                await _run_stage_turn(run)
            except Exception as e:  # noqa: BLE001
                LOG.exception("workflow %s: errore stadio: %s", run_id, e)
    _inflight.discard(run_id)


async def _guarded_resolve(run_id: str) -> None:
    async with _sem:
        run = store.load_run(run_id)
        if run and run["status"] == "await":
            try:
                await _resolve_await(run)
            except Exception as e:  # noqa: BLE001
                LOG.exception("workflow %s: errore resolve: %s", run_id, e)
    _inflight.discard(run_id)


async def tick_once() -> int:
    launched = 0
    for run in store.list_runs(include_done=False):
        rid = run["id"]
        if rid in _inflight:
            continue
        if run["status"] == "pending":
            _inflight.add(rid)
            asyncio.get_running_loop().create_task(_guarded_stage(rid))
            launched += 1
        elif run["status"] == "await" and _has_new_reply(run):
            _inflight.add(rid)
            asyncio.get_running_loop().create_task(_guarded_resolve(rid))
            launched += 1
    return launched


async def engine_loop() -> None:
    LOG.info("workflow engine (conversazionale) avviato (tick %ds)", TICK_SECONDS)
    while True:
        try:
            await tick_once()
        except Exception as e:  # noqa: BLE001
            LOG.warning("workflow tick fallito: %s", e)
        await asyncio.sleep(TICK_SECONDS)
