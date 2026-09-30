"""Il nome di un turno, uguale su tutte e due le sponde (clodia-platform#455).

## Perché esiste

Durante il debug di #423 un 500 del gateway files e un turno OpenCode che
scadeva convivevano nello stesso intervallo di log senza niente in comune: due
tracce indipendenti, e nessun modo di dire se la prima fosse la causa della
seconda o rumore concomitante. La risposta è arrivata leggendo il codice, non i
log — che è esattamente il lavoro che un identificatore condiviso evita.

## Dove nasce, e perché non dentro il turno

Nasce nel dispatcher, con il cronometro del turno (`turn_timing.begin`), non
all'ingresso di `_run_turn`: le chiamate che si vogliono correlare — l'elenco
file del preambolo e il lettore trifecta — girano PRIMA che il turno esista, in
fase di costruzione del prompt. Un id coniato nella sessione arriverebbe un
salto troppo tardi e non etichetterebbe proprio le righe per cui è stato
chiesto.

## Come viaggia

Un `ContextVar`, non un parametro: fra il dispatcher e l'ultima chiamata del
turno ci sono una ventina di firme, e infilarlo in tutte significherebbe che la
ventunesima nascerà senza. Il contesto invece lo seguono da soli i due salti
veri del percorso — `asyncio.to_thread` (il preambolo legge i file in un
thread) e `asyncio.create_task` (il turno gira in un task figlio), che copiano
entrambi il contesto corrente. Il terzo salto, dal dispatcher alla sessione, ha
già la sua consegna esplicita (`turn_timing.bind`/`claim`) e la riusa:
`turn_timing.adopt`.

Verso il gateway il trace esce come header (`X-Clodia-Trace-Id`) da
`GatewayHTTP`, cioè dal punto che TUTTI i client interni attraversano: messo
nei soli header dei topic avrebbe lasciato fuori i client gemelli, e la prima
chiamata non etichettata sarebbe stata indistinguibile da una assente.

## Formato

W3C trace-id: 16 byte casuali in esadecimale. Stesso formato che
`server/audit_events.py` usa per la traccia di audit (clodia-platform#433):
quando quella sarà in `main`, l'unificazione è una riga — il `Turn` adotta
`trace.current()` invece di coniarne uno proprio, e la piattaforma ha un solo
nome per turno invece di due.
"""
from __future__ import annotations

import contextvars
import functools
import secrets

#: L'header con cui il trace attraversa la rete verso il gateway.
HEADER = "X-Clodia-Trace-Id"
#: The W3C header next to it (clodia-platform#463): `00-<trace>-<span>-01`.
TRACEPARENT = "traceparent"

_CURRENT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "clodia_trace_id", default=None)
#: The span of the turn, once the turn has one (`audit_events.Turn.start`).
#: Without it there is no honest `traceparent`: a parent span must name a span
#: that exists, and inventing one would be a correlation nobody can follow.
_SPAN: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "clodia_trace_span", default=None)


def new_id() -> str:
    """Un trace-id nuovo: 16 byte casuali, esadecimale (W3C)."""
    return secrets.token_hex(16)


def bind(trace_id: str | None) -> str | None:
    """Lega (o slega, con `None`) il trace al contesto corrente."""
    _CURRENT.set(trace_id)
    return trace_id


def current() -> str | None:
    """Il trace del turno in corso in QUESTO contesto, o `None` fuori da un turno."""
    return _CURRENT.get()


def bind_span(span_id: str | None) -> None:
    """The turn's span, for the trace currently bound (#463)."""
    t = _CURRENT.get()
    _SPAN.set((t, span_id) if t and span_id else None)


def current_span() -> str | None:
    """The turn's span id — only if it belongs to the trace bound NOW."""
    s = _SPAN.get()
    return s[1] if s and s[0] == _CURRENT.get() else None


def traceparent() -> str | None:
    """W3C `traceparent` of the turn, or None without a trace AND a span."""
    t, s = _CURRENT.get(), current_span()
    return f"00-{t}-{s}-01" if t and s else None


def headers() -> dict[str, str]:
    """Gli header da aggiungere a una chiamata interna.

    Vuoto fuori da un turno: una chiamata della webui non appartiene a nessun
    turno, e darle l'id di quello precedente sarebbe una correlazione inventata
    — peggio di nessuna, perché la si crede.

    With the turn's span also the W3C `traceparent` (#463): the gateway takes
    the trace from it and hangs its events under the turn span.
    """
    t = _CURRENT.get()
    if not t:
        return {}
    out = {HEADER: t}
    tp = traceparent()
    if tp:
        out[TRACEPARENT] = tp
    return out


def tag() -> str:
    """Il trace come si scrive in una riga di log (`-` se non c'è)."""
    return _CURRENT.get() or "-"


def own_turn(fn):
    """Il trace legato DENTRO un dispatcher non gli sopravvive.

    Serve perché i dispatcher si annidano: `_maybe_delegate` avvia il turno di
    un incaricato mentre il turno del delegante sta ancora girando, nello stesso
    task. Senza questo, al ritorno il delegante continuerebbe a chiamare il
    gateway col nome del turno dell'incaricato — una correlazione sbagliata, che
    costa più di una mancante perché la si crede. Il task che il dispatcher
    lascia dietro di sé (`asyncio.create_task`) ha già copiato il contesto col
    trace giusto: ripristinare qui non gli toglie niente.

    Decoratore e non `with`, perché avvolge funzioni da duecento righe: un
    blocco le reindenterebbe tutte, e un diff così non si rilegge.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        precedente, span = _CURRENT.get(), _SPAN.get()
        try:
            return await fn(*args, **kwargs)
        finally:
            _CURRENT.set(precedente)
            _SPAN.set(span)

    return wrapper
