"""Quanto è rimasto fermo l'event loop (clodia-platform#358).

Il 13 set 2026 un turno è rimasto appeso 7h34m e il watchdog l'ha chiuso solo
alla fine, scrivendo «nessun evento SDK da 27276s». La soglia del watchdog è
180s e nei log di esercizio scatta a 187/191/192s: funziona. Quella notte non ha
funzionato perché **non è girato** — e con lui non è girato nemmeno il
`COLLECT_CHUNK_TIMEOUT` da 5 minuti, che è un timer del tutto indipendente. Due
timer asincroni diversi zitti insieme per sette ore non sono due tarature
sbagliate: sono un event loop bloccato da una chiamata sincrona sullo stesso
thread.

Il difetto strutturale che ne segue: un watchdog implementato come task asyncio
sorveglia il prigioniero dalla cella accanto. Se a fermarsi è il thread, non
gira neanche lui — proprio nel caso che produce gli stalli più lunghi, cioè
quello per cui esiste.

Questo modulo non lo risolve: lo MISURA, ed è il passo che viene prima. Il
battito dorme `tick` secondi e guarda quanto tempo è passato davvero; la
differenza è il tempo in cui nessuna coroutine ha potuto girare. È
**retroattivo** per costruzione — mentre il loop è fermo non c'è nessuno che
possa scrivere una riga di log — e va bene così: la riga compare appena il loop
riparte, che è l'unico istante in cui è scrivibile. Senza, la prossima
occorrenza è di nuovo una notte senza prove: quelle dell'incidente sono state
sovrascritte dalla rotazione dei log prima che qualcuno potesse leggerle.

La CURA — un killer fuori dall'event loop, che termini il subprocess anche a
loop bloccato — resta da scrivere, ed è di proposito: un thread daemon in un
processo che oggi non ne ha nessuno è una decisione che va presa su una causa
misurata, non su una ricostruzione.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

LOG = logging.getLogger("agent-server.loop_lag")

#: Ogni quanto il battito si risveglia. Un secondo è abbastanza raro da non
#: pesare e abbastanza fitto da datare un blocco con precisione utile.
LAG_TICK = 1.0
#: Sotto questa deriva non è un blocco: è un loop occupato. Un processo che
#: serve HTTP e turni ha normalmente ritardi di decine o centinaia di ms, e una
#: soglia troppo bassa riempirebbe il log di rumore proprio quando c'è carico —
#: cioè quando lo si legge.
LAG_THRESHOLD = 30.0

# SHORTCUT: gli ultimi 32 blocchi in memoria, per processo. Regge perché a
#           interessare è «il loop era fermo DURANTE questo turno», una domanda
#           sul passato recente. Se servisse la storia lunga (un grafico, una
#           soglia di allarme) il posto non è qui, è la serie temporale
#           dell'osservabilità.
_STALLS: "deque[tuple[float, float]]" = deque(maxlen=32)   # (fine, durata)


def reset() -> None:
    """Dimentica i blocchi registrati. Per i test, e per un riavvio pulito."""
    _STALLS.clear()


def note_stall(fine: float, durata: float) -> None:
    _STALLS.append((fine, durata))
    LOG.error("event loop bloccato per %.0fs: in quella finestra nessun timer "
              "asincrono ha potuto scattare (watchdog di turno compreso)", durata)


def stall_since(inizio: float) -> float:
    """Secondi di blocco del loop **finiti dopo** `inizio`, sulla stessa scala
    di `clock` (di norma `time.monotonic`).

    Un blocco concluso PRIMA della finestra non le appartiene: un turno partito
    dopo che il loop era ripartito non è vittima di quel blocco, e attribuirglielo
    manderebbe la diagnosi successiva a cercare nel posto sbagliato.
    """
    return sum(d for fine, d in _STALLS if fine >= inizio)


async def heartbeat(tick: float = LAG_TICK, threshold: float = LAG_THRESHOLD,
                    giri: int | None = None, sleep=asyncio.sleep,
                    clock=time.monotonic) -> None:
    """Misura la deriva del loop finché non viene cancellato.

    `giri` limita le iterazioni (serve ai test: in esercizio è None = per
    sempre). `sleep`/`clock` sono iniettabili per la stessa ragione — un test
    che aspettasse davvero sette ore non è un test.
    """
    prima = clock()
    n = 0
    try:
        while giri is None or n < giri:
            n += 1
            await sleep(tick)
            adesso = clock()
            deriva = (adesso - prima) - tick
            if deriva >= threshold:
                note_stall(adesso, deriva)
            prima = adesso
    except asyncio.CancelledError:
        pass
