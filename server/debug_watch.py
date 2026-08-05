"""Modalità debug: rilevazione delle anomalie di canale e chiamata a sysadmin.

Il problema che risolve, detto con i fatti raccolti oggi: quando qualcosa non
funziona, l'informazione ESISTE e non arriva a nessuno che possa agire. Un turno
che solleva un'eccezione scrive una riga di log e lascia il canale in silenzio —
per chi guarda, «l'agente è stato menzionato e non risponde». Una delega verso un
agente non idoneo fa `continue`. Un verbo che falla ritorna un errore che l'agente
interpreta come un guasto del server e poi chiede aiuto all'umano.

Tre difetti diversi con la stessa forma: il segnale si ferma prima di chi può
usarlo. La modalità debug chiude quel salto.

## Cosa NON è

Non è un agente che legge la chat. Guardare i messaggi con un modello costerebbe
un turno per messaggio e vedrebbe MENO di quanto già sappiamo: il fallimento di
un turno è un'eccezione, non un'inferenza. La rilevazione qui è **deterministica
e server-side** — nessun token — e un agente viene svegliato solo quando un
segnale è già scattato, con un brief strutturato.

## Perché sysadmin non può stare in tutte le chat

Ha clearance SEAL-1: in un topic SEAL-2+ la membership è rifiutata, ed è la
difesa giusta. Declassificare i topic o promuovere sysadmin per fare diagnostica
sarebbe barattare il modello di sicurezza per la sua osservabilità. Quindi:
partecipante dove la clearance lo consente, e altrove il monitor gira comunque —
non serve stare nella stanza per contarne i fallimenti. I segnali portano
METADATI (nome del verbo, nome dell'agente, tipo di errore), non contenuto: la
stessa regola della telemetria dei verbi, per la stessa ragione.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

LOG = logging.getLogger("agent-server.debug-watch")

#: L'agente che interviene. Non configurabile per capriccio: è quello che ha i
#: verbi per agire (`runtime.restart_agent`, `app_runtime.*`) E per aprire una
#: issue (`github.issue_write`). Un guardiano senza mani segnala e nient'altro.
WATCHER = "sysadmin"

#: Anti-loop: un fallimento del guardiano non deve generare un segnale che
#: sveglia il guardiano. È la ricorsione che trasforma un guasto in una tempesta.
_NEVER_SUBJECT = {WATCHER}

#: Finestra di soppressione per (kind, canale, soggetto). Lo stesso guasto si
#: ripete a ogni tentativo: senza questa, un provider giù riempirebbe il canale
#: di segnalazioni identiche — cioè esattamente il rumore che rende inutile un
#: allarme.
DEDUP_SECONDS = int(os.environ.get("CLODIA_DEBUG_DEDUP_SECONDS", "600"))

_seen: dict[tuple, float] = {}


def enabled() -> bool:
    """Modalità debug attiva? Spenta per default.

    Accesa costa: sysadmin entra nei canali dove può, e ogni anomalia sveglia un
    turno. È una modalità da tenere accesa quando si sta cercando un guasto, non
    un default — e dirlo qui evita che diventi un default per inerzia.
    """
    return (os.environ.get("CLODIA_DEBUG_MODE") or "").strip().lower() in ("1", "true", "on", "yes")


@dataclass
class Anomaly:
    """Un'anomalia rilevata. Metadati, mai contenuto."""
    kind: str
    channel: str
    subject: str = ""          # l'agente coinvolto
    detail: str = ""           # causa tecnica, già leggibile
    evidence: dict = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def brief(self) -> str:
        """Il brief per il guardiano: cosa è successo, e cosa gli si chiede.

        Contiene ENTRAMBE le uscite perché sono entrambe legittime: si ripara, o
        si documenta perché non si può riparare. Un brief che chiede solo di
        riparare produce tentativi improvvisati quando la causa è nel codice; uno
        che chiede solo di aprire una issue rimanda anche ciò che si sistemava con
        un restart.
        """
        ev = "; ".join(f"{k}={v}" for k, v in self.evidence.items() if v) or "—"
        return (
            f"[ANOMALIA · {self.kind}] Canale {self.channel}"
            + (f" · agente {self.subject}" if self.subject else "")
            + f"\n{self.detail}\nEvidenza: {ev}\n\n"
            "Sei intervenuto perché la modalità debug è attiva. Due esiti "
            "possibili, e vanno distinti:\n"
            "1. È riparabile ora — un agente da riavviare (runtime.restart_agent), "
            "un servizio da controllare (app_runtime.health), un provider non "
            "connesso: fallo, poi scrivi in canale COSA hai fatto e se il "
            "sintomo è cessato.\n"
            "2. Non è riparabile ora — la causa è nel codice o nella "
            "configurazione: apri una issue su r-clodia/clodia-platform "
            "(github.issue_write) con il sintomo, l'evidenza qui sopra e come "
            "riprodurlo, poi scrivi in canale il link.\n\n"
            "Non chiedere a Davide cosa fare prima di aver guardato: "
            "l'evidenza che hai è già più di quella che aveva chi ha "
            "segnalato. Se dopo aver guardato serve una decisione sua, "
            "chiedila dicendo cosa hai escluso."
        )


def _dedup_key(a: Anomaly) -> tuple:
    return (a.kind, a.channel, a.subject)


def should_report(a: Anomaly) -> bool:
    """False se è un doppione recente, o se il soggetto è il guardiano stesso."""
    if a.subject in _NEVER_SUBJECT:
        LOG.info("anomalia %s su %s ignorata: il soggetto è il guardiano",
                 a.kind, a.channel)
        return False
    key = _dedup_key(a)
    now = time.time()
    last = _seen.get(key)
    if last is not None and (now - last) < DEDUP_SECONDS:
        return False
    _seen[key] = now
    return True


def reset_dedup() -> None:
    """Solo per i test: la finestra di soppressione è stato globale."""
    _seen.clear()
