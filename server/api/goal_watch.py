"""Un obiettivo fermo torna a bussare (clodia-platform#457).

L'obiettivo di un canale è un **requisito**, non un tentativo: se l'orchestratore
muore a metà — crash della sessione, riavvio del server, turno ucciso da un
timeout — il goal resta scritto nel meta e non succede più niente. Nessuno se ne
accorge: la stanza è silenziosa e il silenzio somiglia al lavoro in corso.

La domanda della issue, alla lettera: «is_the_goal_reached == False AND
are_agents_working_on_it == FALSE» → risveglia l'orchestratore.

**Da quanto è fermo si legge da `updated_at` del canale**, non da un timestamp
nuovo: ogni messaggio lo muove, quindi un canale che lavora non è mai fermo, e
il risveglio stesso — che posta — lo azzera. Aggiungere un `nudged_at` avrebbe
voluto dire un campo di stato in più che può divergere dalla realtà; qui il
dato è già vero per costruzione.
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


def obiettivi_fermi(righe, *, now: datetime | None = None,
                    fermo_da_minuti: float | None = None,
                    occupato=lambda tier, name: False) -> list[dict]:
    """I canali con un obiettivo aperto su cui non sta lavorando nessuno.

    Funzione PURA sulle righe della lista topic (più il predicato `occupato`):
    la decisione si può provare senza un server, senza un gateway e senza
    postare niente in una stanza vera.
    """
    now = now or datetime.now(timezone.utc)
    limite = timedelta(minutes=fermo_da_minuti if fermo_da_minuti is not None
                       else _fermo_da_minuti())
    out: list[dict] = []
    for r in righe or []:
        goal = r.get("goal")
        if not isinstance(goal, dict) or not str(goal.get("text") or "").strip():
            continue
        if str(goal.get("state") or "pinned") not in STATI_DI_LAVORO:
            continue
        tier, name = str(r.get("tier") or ""), str(r.get("name") or "")
        if not tier or not name:
            continue
        # Archiviato o chiuso: l'obiettivo è appeso a una stanza che non è più
        # in esercizio. Non è il watchdog a doverlo risolvere.
        if str(r.get("status") or "active") in ("archived", "done"):
            continue
        quando = _quando(r.get("updated_at"))
        if quando is None or now - quando < limite:
            continue
        if occupato(tier, name):
            continue  # qualcuno ci sta lavorando ADESSO: non è fermo
        out.append({"tier": tier, "name": name, "goal": goal,
                    "fermo_da_minuti": round((now - quando).total_seconds() / 60)})
    return out


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
    return (f"⏰ **Obiettivo ancora aperto**, e in questo canale non si muove niente "
            f"da {voce['fermo_da_minuti']} minuti.\n\n"
            f"> {goal.get('text', '')}\n\n"
            f"Stato: `{stato}`. {mossa}\n\n"
            "Se l'obiettivo è già raggiunto, dichiaralo con "
            "`topic.goal_progress(state=\"claimed-done\")` invece di lasciarlo aperto.")


async def tick() -> dict:
    """Un giro di sorveglianza. Ritorna cosa ha fatto (utile ai test e ai log)."""
    from . import channels, topics_client

    try:
        righe = await topics_client.async_list_topics()
    except Exception as e:  # noqa: BLE001
        LOG.warning("goal watch: lista topic non disponibile (%s)", e)
        return {"esaminati": 0, "risvegliati": []}

    fermi = obiettivi_fermi(righe, occupato=channels._qualcuno_al_lavoro)
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
            await channels.post_channel_message(
                tier, name, f"@{orchestratore} {promemoria(voce)}", "system",
                kind="system", trusted_internal=True, skip_if_busy=True)
            risvegliati.append(f"{tier}/{name}")
            LOG.info("goal watch: risvegliato %s su %s/%s (fermo da %s min)",
                     orchestratore, tier, name, voce["fermo_da_minuti"])
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
