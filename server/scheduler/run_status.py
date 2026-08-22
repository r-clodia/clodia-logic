"""Lo stato di uscita che l'AGENTE dichiara per il proprio run.

Prima di questo modulo lo stato di un run agentico era il valore di verità di
`await chat.send_user_message(prompt)`: `success` se la chiamata non solleva,
`failed` se solleva. Cioè misurava che il TURNO fosse terminato, non che il
LAVORO fosse stato fatto — e le due cose divergono ogni volta che il guasto è a
valle dell'avvio del turno.

Il caso che ha aperto la questione (clodia-platform#206, misurato il 22 ago
2026): il job «Daily digest GRC» ha girato 652 secondi, ha tentato `email.send`
tre volte, ha fallito tre volte perché il refresh OAuth della casella rispondeva
`invalid_grant`, e ha registrato **`success`**. Lo stato era fedele a ciò che
misurava; era la misura a essere sbagliata.

## I quattro stati, e perché non sono tre

    success   il lavoro è stato fatto
    error     è stato fatto, ma qualcosa è andato storto e la QUALITÀ del
              risultato può esserne compromessa — tre fonti su cinque in 403, un
              allegato non recuperato, metà del digest
    fatal     il turno è arrivato a termine e il lavoro NON è stato fatto:
              l'agente lo sa e lo dichiara
    failed    il turno è morto — eccezione, timeout, provider non raggiungibile

`fatal` NON sostituisce `failed`: sono cose diverse, e sovrapporle perderebbe
esattamente l'informazione che questo modulo aggiunge. `failed` lo constata
l'infrastruttura, `fatal` lo dichiara l'agente. Un job che non parte e un job che
parte, lavora e conclude di non avere nulla da consegnare vanno letti in modo
diverso, e chi li legge di solito sta cercando la seconda.

## Assenza di dichiarazione = `error`, non `success`

Decisione dell'owner, 22 ago 2026. Un run che non dichiara nulla è un run di cui
non sappiamo l'esito, e «non lo so» non è «è andata bene»: quello è il difetto di
partenza. Il costo è dichiarato — i job che non chiamano il verbo passano a
`error` finché il loro prompt non viene aggiornato — e va preferito al suo
opposto, che è un elenco di run verdi che nessuno rilegge.

## Perché in memoria

La dichiarazione vive fra l'inizio e la fine dello stesso turno, nello stesso
processo che lo esegue: se l'agent-server muore a metà, il turno è perso comunque
e non c'è nulla da concludere. Persisterla su disco introdurrebbe uno stato da
riconciliare al boot senza rispondere a nessuna domanda in più.
"""
from __future__ import annotations

import threading
from typing import Optional

#: Stati che un AGENTE può dichiarare. Insieme chiuso: uno stato inventato non
#: viene registrato come tale — verrebbe reso `unknown` dalla UI e letto come
#: «boh» proprio dove serve una risposta. `failed` non è qui di proposito: non è
#: dichiarabile, lo constata l'infrastruttura quando il turno muore.
DECLARABLE = ("success", "error", "fatal")

#: Stato di un run che è arrivato alla fine senza che nessuno ne dichiarasse
#: l'esito. Vedi il docstring del modulo: non è `success`.
UNDECLARED = "error"

UNDECLARED_DETAIL = ("l'agente non ha dichiarato l'esito del run "
                     "(jobs.report_status non è stato chiamato)")

# chat_id → (status, detail). Una sola dichiarazione per turno: l'ultima vince,
# perché un agente che si corregge sta dicendo qualcosa di più aggiornato, non
# qualcosa di meno vero.
_DICHIARATI: dict[str, tuple[str, str | None]] = {}
_LOCK = threading.Lock()


def declare(chat_id: str, status: str, detail: Optional[str] = None) -> str:
    """Registra lo stato dichiarato per il turno `chat_id`. Ritorna lo stato
    normalizzato.

    Solleva `ValueError` su uno stato fuori da `DECLARABLE`: un errore azionabile
    che elenca i valori validi vale più di una registrazione silenziosa di
    qualcosa che nessun lettore saprà interpretare.
    """
    cid = str(chat_id or "").strip()
    if not cid:
        raise ValueError("chat_id richiesto per dichiarare lo stato di un run")
    s = str(status or "").strip().lower()
    if s not in DECLARABLE:
        raise ValueError(
            f"stato '{status}' non dichiarabile; ammessi: {', '.join(DECLARABLE)}"
            " ('failed' non è dichiarabile: lo constata l'infrastruttura)")
    d = (str(detail).strip() or None) if detail is not None else None
    with _LOCK:
        _DICHIARATI[cid] = (s, d)
    return s


def take(chat_id: str) -> tuple[str, str | None]:
    """Consuma la dichiarazione del turno `chat_id`.

    Consuma invece di leggere perché una dichiarazione vale per UN run: lasciarla
    lì farebbe ereditare al run successivo dello stesso job l'esito del
    precedente — che è il difetto della callback lasciata attaccata alla sessione
    (`on_visible_block`), già visto in `api/channels.py`.

    Senza dichiarazione ritorna `(UNDECLARED, UNDECLARED_DETAIL)`.
    """
    cid = str(chat_id or "").strip()
    with _LOCK:
        got = _DICHIARATI.pop(cid, None)
    if got is None:
        return (UNDECLARED, UNDECLARED_DETAIL)
    return got


def peek(chat_id: str) -> tuple[str, str | None] | None:
    """Lo stato dichiarato senza consumarlo, o None. Per la sola osservabilità."""
    with _LOCK:
        return _DICHIARATI.get(str(chat_id or "").strip())


def forget(chat_id: str) -> None:
    """Scarta una dichiarazione pendente. Serve quando un turno viene abbandonato
    senza passare dal completamento: senza questo, la dichiarazione resterebbe in
    memoria fino al prossimo run con lo stesso chat_id."""
    with _LOCK:
        _DICHIARATI.pop(str(chat_id or "").strip(), None)


def pending_count() -> int:
    """Quante dichiarazioni non ancora consumate. Per i test e la diagnostica: se
    cresce senza scendere, c'è un turno che non passa dal completamento."""
    with _LOCK:
        return len(_DICHIARATI)
