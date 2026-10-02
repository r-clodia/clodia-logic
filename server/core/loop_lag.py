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
import os
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


#: Quanto indietro si guarda quando il guasto da correlare NON ha un istante di
#: inizio proprio — una sessione trovata invalida, un subprocess ucciso: cose
#: che si scoprono, non che si cronometrano.
#:
#: Due ore perché i due casi misurati hanno proprio questa forma: in
#: clodia-platform#473 il blocco finisce alle 05:58 e la sessione opencode
#: risulta invalida alle 06:52 (54 minuti dopo); in #492 il blocco finisce alle
#: 18:06 e il SIGKILL arriva alle 18:57 (51 minuti dopo). Un'ora li prenderebbe
#: entrambi per pochi minuti, cioè per caso.
FINESTRA_CORRELAZIONE = float(os.environ.get("CLODIA_LAG_CORRELAZIONE", "7200"))


def ultimo_blocco(entro: float = FINESTRA_CORRELAZIONE, *,
                  clock=time.monotonic) -> "tuple[float, float] | None":
    """L'ultimo blocco **concluso** negli ultimi `entro` secondi, come
    `(durata, quanti secondi fa è finito)`. `None` se non ce n'è.

    Diverso da `stall_since`, e la differenza è la domanda. `stall_since`
    risponde a «il loop si è fermato DURANTE questa cosa», e vuole l'istante in
    cui la cosa è cominciata. Qui la domanda è «il loop si era fermato poco
    PRIMA», che è quella che si pone davanti a una sessione trovata invalida o a
    un processo ucciso: lì un istante di inizio non c'è.
    """
    ora = clock()
    for fine, durata in reversed(_STALLS):
        if 0 <= ora - fine <= entro:
            return durata, ora - fine
    return None


def nota_blocco(entro: float = FINESTRA_CORRELAZIONE, *,
                clock=time.monotonic) -> str:
    """La correlazione in una frase, pronta da appendere a un messaggio, o `""`.

    Esiste perché clodia-platform#473 chiede esattamente questo: i blocchi del
    loop e le sessioni invalidate «andrebbero loggate come causa-effetto
    esplicita, non dedotte a mano dal timestamp». Dedurle a mano ha richiesto
    un'indagine su due notti di log, e chi legge il messaggio d'errore non la fa.

    La frase dichiara una CONCOMITANZA e i suoi numeri, non una causa: il loop
    bloccato poco prima è compatibile con un riavvio o una sospensione della
    piattaforma, e per decidere servono gli altri indizi. Dirlo come causa certa
    sarebbe l'errore che la #358 ha già pagato con sette ore cercate dalla parte
    sbagliata.
    """
    b = ultimo_blocco(entro, clock=clock)
    if b is None:
        return ""
    durata, fa = b
    return (f"l'event loop si era bloccato {durata:.0f}s, finito {fa:.0f}s prima: "
            f"nella stessa finestra la piattaforma può essersi fermata o riavviata")


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
