"""Un obiettivo fermo torna a bussare (clodia-platform#457).

L'obiettivo di un canale è un **requisito**, non un tentativo: se l'orchestratore
muore a metà — crash della sessione, riavvio del server, turno ucciso da un
timeout — il goal resta scritto nel meta e non succede più niente. Nessuno se ne
accorge: la stanza è silenziosa e il silenzio somiglia al lavoro in corso.

La domanda della issue, alla lettera: «is_the_goal_reached == False AND
are_agents_working_on_it == FALSE» → risveglia l'orchestratore.

**How long it has been still is measured from the channel's activity**: the
last message in the channel (read from the gateway, so it includes what agents
post directly through `topic.post_message`) and the last turn that ended here
in this process. NOT from the topic list's `updated_at`: on the gateway that is
max(meta, summary, AGENTS.md) and does not move with messages, so a busy room
looked stale 45 minutes after its last meta change and was pinged at every
tick. The reminder is itself a message, so it resets the clock.

**Bounded by construction.** Each goal gets at most `CLODIA_GOAL_MAX_REMINDERS`
(default 3) reminders without new human activity or a change in the goal
itself, and the silence required before the next one doubles each time
(45, 90, 180 min). An agent answering the reminder does not reset the counter:
otherwise a ping → reply → silence cycle would ping forever. The counter lives
in memory: a restart gives at most one more series, not a loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

LOG = logging.getLogger("agent-server.api.goal_watch")

#: Stati in cui il lavoro è degli AGENTI. Negli altri l'obiettivo aspetta
#: l'owner (`strategy-review`, `claimed-done`) o è chiuso (`done`): insistere
#: lì non sveglierebbe chi deve muoversi, ma chi non ha niente da fare.
STATI_DI_LAVORO = ("pinned", "in-progress")

#: Quanto silenzio, su un canale con un obiettivo aperto, è troppo. Generoso di
#: proposito: un turno lungo che pensa senza scrivere non deve essere scambiato
#: per un turno morto, e l'errore costoso è il falso positivo — un secondo
#: orchestratore lanciato sullo stesso piano.
def _fermo_da_minuti() -> float:
    return float(os.environ.get("CLODIA_GOAL_STALE_MIN", "45"))


def _tick_secondi() -> float:
    return float(os.environ.get("CLODIA_GOAL_WATCH_TICK_SEC", "600"))


def _quando(valore) -> datetime | None:
    if not valore:
        return None
    try:
        d = datetime.fromisoformat(str(valore).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _max_promemoria() -> int:
    return int(os.environ.get("CLODIA_GOAL_MAX_REMINDERS", "3"))


#: Marker of the watcher's own reminder (it opens every reminder text).
MARCA = "⏰ **Obiettivo ancora aperto**"


def firma(goal: dict) -> tuple:
    """What identifies the goal AND its progress: a new text, state or plan is
    a goal that moved, and its reminder series starts over."""
    return (str(goal.get("text") or "").strip(), str(goal.get("state") or "pinned"),
            str(goal.get("strategy_path") or ""))


def con_obiettivo_aperto(r: dict) -> bool:
    """A row with an open goal whose next move belongs to the agents."""
    goal = r.get("goal")
    if not isinstance(goal, dict) or not str(goal.get("text") or "").strip():
        return False
    if str(goal.get("state") or "pinned") not in STATI_DI_LAVORO:
        return False
    if not str(r.get("tier") or "") or not str(r.get("name") or ""):
        return False
    # Archiviato o chiuso: l'obiettivo è appeso a una stanza che non è più
    # in esercizio. Non è il watchdog a doverlo risolvere.
    return str(r.get("status") or "active") not in ("archived", "done")


def obiettivi_fermi(righe, *, now: datetime | None = None,
                    fermo_da_minuti: float | None = None,
                    occupato=lambda tier, name: False,
                    attivita: dict | None = None,
                    solleciti: dict | None = None,
                    max_promemoria: int | None = None) -> list[dict]:
    """I canali con un obiettivo aperto su cui non sta lavorando nessuno.

    Funzione PURA sulle righe della lista topic (più il predicato `occupato`):
    la decisione si può provare senza un server, senza un gateway e senza
    postare niente in una stanza vera.

    `attivita[(tier, name)]` is the channel's last activity (message or turn
    end); a channel without it is never declared stale — not knowing how long
    it has been still is not evidence that it is. `solleciti[(tier, name)]` is
    the reminder state of the goal (`firma`, `count`, `last_ping`).
    """
    now = now or datetime.now(timezone.utc)
    limite = timedelta(minutes=fermo_da_minuti if fermo_da_minuti is not None
                       else _fermo_da_minuti())
    tetto = max_promemoria if max_promemoria is not None else _max_promemoria()
    attivita, solleciti = attivita or {}, solleciti or {}
    out: list[dict] = []
    for r in righe or []:
        if not con_obiettivo_aperto(r):
            continue
        tier, name, goal = str(r["tier"]), str(r["name"]), r["goal"]
        st = solleciti.get((tier, name))
        if st is not None and st.get("firma") != firma(goal):
            st = None  # the goal moved: a new series
        count = int((st or {}).get("count") or 0)
        if count >= tetto:
            continue  # cap reached: wait for a human or for the goal to move
        quando = _quando(attivita.get((tier, name)))
        if quando is None:
            continue
        ultimo_ping = _quando((st or {}).get("last_ping"))
        if ultimo_ping is not None and ultimo_ping > quando:
            quando = ultimo_ping  # the wake-up resets the clock
        # Backoff: each reminder without an answer doubles the silence needed.
        if now - quando < limite * (2 ** count):
            continue
        if occupato(tier, name):
            continue  # qualcuno ci sta lavorando ADESSO: non è fermo
        out.append({"tier": tier, "name": name, "goal": goal, "promemoria_n": count + 1,
                    "fermo_da_minuti": round((now - quando).total_seconds() / 60)})
    return out


def attivita_da_messaggi(messaggi: list[dict]) -> tuple[datetime | None, datetime | None]:
    """(last message of any kind, last HUMAN message) from a channel's messages.
    The watcher's own reminders count as activity (they reset the clock) but
    never as human activity."""
    ultimo = umano = None
    for m in messaggi or []:
        t = _quando(m.get("ts") or m.get("created_at"))
        if t is None:
            continue
        if ultimo is None or t > ultimo:
            ultimo = t
        testo = str(m.get("text") or m.get("content") or "")
        if (str(m.get("kind") or "") == "human" and str(m.get("author") or "") != "system"
                and MARCA not in testo and (umano is None or t > umano)):
            umano = t
    return ultimo, umano


#: Reminder state per channel, in memory: {(tier, name): {firma, count, last_ping}}.
_SOLLECITI: dict[tuple[str, str], dict] = {}


def promemoria(voce: dict) -> str:
    """Il testo del risveglio. Dice da quanto è fermo, perché è arrivato e
    qual è la mossa successiva: un «ricordati» senza contesto costringe a
    ricostruire la storia, e chi lo riceve potrebbe essere uno spawn nuovo che
    quella storia non l'ha mai vista."""
    goal = voce["goal"]
    stato = str(goal.get("state") or "pinned")
    piano = goal.get("strategy_path")
    if stato == "pinned":
        mossa = ("Scrivi la strategia e dichiarala con "
                 "`topic.goal_progress(state=\"strategy-review\", strategy_path=\"<path>\")`.")
    else:
        mossa = (f"Riprendi dal piano in `{piano}`" if piano
                 else "Riprendi il piano approvato") + (
            ", passo per passo. Se sei bloccato, dillo in una riga dicendo cosa ti serve.")
    return (f"{MARCA}, e in questo canale non si muove niente "
            f"da {voce['fermo_da_minuti']} minuti.\n\n"
            f"> {goal.get('text', '')}\n\n"
            f"Stato: `{stato}`. {mossa}\n\n"
            "Se l'obiettivo è già raggiunto, dichiaralo con "
            "`topic.goal_progress(state=\"claimed-done\")` invece di lasciarlo aperto.")


async def _attivita_del_canale(tier: str, name: str):
    """(last activity, last human activity) of one channel, or (None, None)."""
    from . import channels, topics_client
    try:
        messaggi = await topics_client.async_list_messages(tier, name, limit=50)
    except Exception as e:  # noqa: BLE001
        LOG.info("goal watch %s/%s: messaggi non disponibili (%s)", tier, name, e)
        return None, None
    ultimo, umano = attivita_da_messaggi(messaggi)
    turno = channels.ultimo_turno_finito(tier, name)
    if turno is not None and (ultimo is None or turno > ultimo):
        ultimo = turno
    return ultimo, umano


async def tick(*, now: datetime | None = None) -> dict:
    """Un giro di sorveglianza. Ritorna cosa ha fatto (utile ai test e ai log)."""
    from . import channels, topics_client

    try:
        righe = await topics_client.async_list_topics()
    except Exception as e:  # noqa: BLE001
        LOG.warning("goal watch: lista topic non disponibile (%s)", e)
        return {"esaminati": 0, "risvegliati": []}

    now = now or datetime.now(timezone.utc)
    candidati = [r for r in (righe or []) if con_obiettivo_aperto(r)]
    attivita: dict = {}
    for r in candidati:
        chiave = (str(r["tier"]), str(r["name"]))
        ultimo, umano = await _attivita_del_canale(*chiave)
        attivita[chiave] = ultimo
        st = _SOLLECITI.get(chiave)
        # A person spoke after the last reminder: the series starts over.
        if st and umano is not None and (_quando(st.get("last_ping")) or now) < umano:
            _SOLLECITI.pop(chiave, None)
    fermi = obiettivi_fermi(candidati, now=now, occupato=channels._qualcuno_al_lavoro,
                            attivita=attivita, solleciti=_SOLLECITI)
    risvegliati = []
    for voce in fermi:
        tier, name = voce["tier"], voce["name"]
        try:
            topic = await topics_client.async_open_topic(tier, name) or {}
            meta = topic.get("meta") or {}
            orchestratore = str(meta.get("contact_agent") or "clodia").strip()
            partecipanti = meta.get("participants") or []
            dentro = (orchestratore in (partecipanti.keys()
                                        if isinstance(partecipanti, dict) else partecipanti)
                      or orchestratore == meta.get("owner"))
            if not dentro:
                # Nessuno da svegliare: insistere ogni tick riempirebbe la
                # stanza di promemoria che nessuno può raccogliere.
                LOG.info("goal watch %s/%s: nessun orchestratore nella stanza (%s)",
                         tier, name, orchestratore)
                continue
            # Counted BEFORE posting: a post that fails half-way must not turn
            # into an uncounted retry at every tick.
            _SOLLECITI[(tier, name)] = {"firma": firma(voce["goal"]),
                                        "count": voce["promemoria_n"],
                                        "last_ping": now.isoformat()}
            await channels.post_channel_message(
                tier, name, f"@{orchestratore} {promemoria(voce)}", "system",
                kind="system", trusted_internal=True, skip_if_busy=True)
            risvegliati.append(f"{tier}/{name}")
            LOG.info("goal watch: risvegliato %s su %s/%s (fermo da %s min, promemoria %s/%s)",
                     orchestratore, tier, name, voce["fermo_da_minuti"],
                     voce["promemoria_n"], _max_promemoria())
        except Exception as e:  # noqa: BLE001
            # Un canale che non si apre non deve fermare la sorveglianza degli
            # altri: il watchdog serve proprio quando qualcosa è rotto.
            LOG.warning("goal watch %s/%s: %s", tier, name, e)
    return {"esaminati": len(righe), "risvegliati": risvegliati}


async def loop() -> None:
    """Task di fondo: un tick ogni `CLODIA_GOAL_WATCH_TICK_SEC` (10 min).
    `CLODIA_GOAL_STALE_MIN <= 0` lo spegne — e lo dice, perché una sorveglianza
    spenta in silenzio è peggio che non averla."""
    if _fermo_da_minuti() <= 0:
        LOG.warning("goal watch DISABILITATO (CLODIA_GOAL_STALE_MIN<=0): "
                    "un obiettivo fermo non verrà più ripreso da solo")
        return
    LOG.info("goal watch attivo: tick=%.0fs, fermo oltre %.0f min",
             _tick_secondi(), _fermo_da_minuti())
    while True:
        await asyncio.sleep(_tick_secondi())
        try:
            await tick()
        except Exception as e:  # noqa: BLE001
            LOG.warning("goal watch tick: %s", e)
