"""Le fasi che stanno PRIMA del primo token di un turno (clodia-platform#330).

Il silenzio iniziale di un turno viene attribuito per intuizione al contesto da
rileggere («400k token prima della prima parola»), ma fra il messaggio accettato
e la `query` al modello ci sono almeno cinque attese seriali che nessuno
misurava: la scelta del risponditore, l'attesa di uno spawn libero, la creazione
della sessione, la costruzione del prompt (che legge la storia dal gateway) e la
coda sul lock della sessione. Ottimizzare senza questi numeri vuol dire scegliere
a caso la fase da sistemare.

Il cronometro nasce nel dispatcher, dove il turno comincia davvero, e si chiude
nella sessione, al primo delta che arriva dal modello.

Dentro `channels.py` l'oggetto viaggia **esplicito**, di parametro in parametro.
Il passaggio dal dispatcher alla sessione, invece, non ha una firma: le tre
classi di sessione (Chat/Codex/OpenCode) condividono `send_user_message(prompt)`
e i fake dei test la implementano, quindi aggiungere un argomento là romperebbe
chi non lo conosce — la stessa ragione per cui `on_visible_block` (#243) è un
attributo e non un parametro. Per quel solo salto c'è la consegna qui sotto:

    t = turn_timing.begin("start_turn")
    t.mark("routing")                    # ... nel dispatcher
    t.bind(chat_id)                      # subito prima di `send_user_message`
    ...
    t = turn_timing.claim(chat_id)       # nella sessione, preso il lock
    t.mark("queue_wait")
    turn_timing.first_token(t)           # al primo delta: scrive la riga
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict, deque

LOG = logging.getLogger("agent-server.turn_timing")

# SHORTCUT: consegna in memoria, per processo, con un tetto sulle chiavi.
#           Regge perché dispatcher e sessione girano nello stesso processo.
#           Le voci di una chiave sono una CODA e si ritirano in ordine di
#           arrivo, che è l'ordine in cui i turni prendono il lock della
#           sessione (`asyncio.Lock` è FIFO): due turni accodati sulla stessa
#           sessione ritirano quindi ognuno il proprio. Se i dispatch della
#           stessa sessione arrivassero fuori ordine, i millisecondi passano da
#           un turno all'altro — una misura imprecisa, non un turno rotto. Se i
#           turni uscissero dal processo, la consegna va sostituita da un trace
#           id propagato, non allargata qui.
_MAX_KEYS = 256
_PENDING: "OrderedDict[str, deque[TurnTiming]]" = OrderedDict()


class TurnTiming:
    """Le durate delle fasi di UN turno, in ordine di attraversamento."""

    __slots__ = ("origin", "t0", "phases", "chat_id", "_last", "_closed")

    def __init__(self, origin: str) -> None:
        self.origin = origin
        # `monotonic`: misura durate, e non deve poter andare indietro se
        # l'orologio di sistema viene corretto mentre un turno è in volo.
        self.t0 = time.monotonic()
        self._last = self.t0
        self.phases: list[tuple[str, float]] = []
        self.chat_id: str | None = None
        self._closed = False

    def mark(self, phase: str) -> None:
        """Chiude la fase corrente e la registra in millisecondi."""
        now = time.monotonic()
        self.phases.append((phase, (now - self._last) * 1000.0))
        self._last = now

    def bind(self, chat_id: str | None) -> None:
        """Mette il turno in consegna per la sessione `chat_id`.

        Si chiama tardi, subito prima dell'invio: fra il `bind` e il `claim`
        della sessione c'è solo l'attesa del lock, cioè esattamente la fase che
        la sessione misura per prima.
        """
        if not chat_id:
            return
        self.chat_id = chat_id
        coda = _PENDING.get(chat_id)
        if coda is None:
            coda = _PENDING[chat_id] = deque()
        coda.append(self)
        _PENDING.move_to_end(chat_id)
        while len(_PENDING) > _MAX_KEYS:
            _PENDING.popitem(last=False)

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.t0) * 1000.0

    def line(self) -> str:
        """La riga di telemetria: totale, fasi note, e il resto non attribuito.

        `model_ms` è ciò che avanza fra l'ultima fase misurata e il primo token:
        con `query_sent` come ultima fase è il tempo del modello, cioè proprio la
        quantità che la #228 ipotizza dominante. Si ricava per differenza invece
        di essere cronometrata a parte perché così la somma delle voci fa sempre
        il totale, e una fase che dimenticassimo di marcare si vede — finisce
        dentro `model_ms` e lo gonfia, invece di scomparire.
        """
        totale = self.elapsed_ms()
        noto = sum(ms for _, ms in self.phases)
        pezzi = [f"{fase}_ms={ms:.0f}" for fase, ms in self.phases]
        pezzi.append(f"model_ms={max(totale - noto, 0.0):.0f}")
        return (f"TTFT {self.chat_id or '-'} origin={self.origin} "
                f"ttft_ms={totale:.0f} " + " ".join(pezzi))


def begin(origin: str) -> TurnTiming:
    """Apre il cronometro di un turno. `origin` = quale dispatcher lo avvia."""
    return TurnTiming(origin)


def claim(chat_id: str | None) -> TurnTiming | None:
    """La sessione ritira il turno in consegna, o `None` se non ce n'è.

    Ritira davvero: la voce esce dalla consegna, così il turno appartiene a un
    solo cronometro e nessun altro può chiuderlo. `None` non è un errore — un
    turno che non passa dai dispatcher (verbo diretto sulla sessione, test) non
    ha fasi da raccontare.
    """
    coda = _PENDING.get(chat_id) if chat_id else None
    if not coda:
        return None
    t = coda.popleft()
    if not coda:
        _PENDING.pop(chat_id, None)  # type: ignore[arg-type]
    return t


def drop(t: TurnTiming | None) -> None:
    """Ritira dalla consegna un turno che non arriverà alla sessione.

    Serve quando l'invio fallisce prima del `claim` (sessione mai avviata,
    provider caduto): senza, la voce resterebbe in coda e il turno SUCCESSIVO
    sulla stessa sessione ritirerebbe un cronometro partito minuti prima,
    scrivendo un TTFT enorme e inventato. Una misura sbagliata è peggio di una
    misura assente, perché la si crede.
    """
    if t is None or not t.chat_id:
        return
    coda = _PENDING.get(t.chat_id)
    if not coda:
        return
    try:
        coda.remove(t)
    except ValueError:
        return  # già ritirato dalla sessione: è il caso normale
    if not coda:
        _PENDING.pop(t.chat_id, None)


def mark(t: TurnTiming | None, phase: str) -> None:
    """Registra una fase, tollerando l'assenza del cronometro.

    Tollera perché la telemetria non deve poter rompere un turno: i percorsi
    senza dispatcher esistono e sono legittimi.
    """
    if t is not None:
        t.mark(phase)


def first_token(t: TurnTiming | None) -> None:
    """Il silenzio è finito: scrive la riga, una volta per turno."""
    if t is None or t._closed:
        return
    t._closed = True
    try:
        LOG.info("%s", t.line())
    except Exception:  # noqa: BLE001 — una misura non fa cadere un turno
        pass


def pending(chat_id: str | None = None) -> int:
    """Quante consegne sono aperte (per chiave, o in tutto). Per i test."""
    if chat_id is not None:
        return len(_PENDING.get(chat_id) or ())
    return sum(len(c) for c in _PENDING.values())
