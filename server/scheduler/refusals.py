"""I verbi NEGATI dentro un turno, perché arrivino nel run record col loro nome.

Gemello di `run_status.py`, e la differenza fra i due è tutta qui: lì l'esito lo
**dichiara** l'agente, qui il rifiuto lo **constata** l'infrastruttura. Due
sorgenti diverse per la stessa riga di storico, e nessuna delle due sostituisce
l'altra — un agente può dichiarare `success` in buona fede su un lavoro che non
è avvenuto, ed è precisamente il caso di clodia-platform#206.

## Cosa restava aperto dopo `#341`

`#341` ha già togliato il difetto peggiore: un run che non dichiara niente è
`error`, non `success`. Restavano due cose:

1. un `success` **dichiarato** copre il rifiuto. `db.complete_run` valorizza il
   dettaglio solo per gli stati `NOT_OK`, quindi su `success` il fatto non lascia
   traccia in nessun punto dello storico;
2. nessun run record nomina mai il verbo. «l'agente non ha dichiarato l'esito» è
   vero e non azionabile; `email.send negato (denied_tools)` lo è.

L'informazione esisteva già in entrambi i punti in cui un rifiuto viene
**deciso** — non dedotto:

- `sdk_runtime.session._permission_gate`: il diniego sui `native_tools` che il
  seed non dichiara lo decidiamo qui dentro;
- il **gateway** (`clodia-tools`, ramo `except PermissionError` del dispatch):
  già calcola la classe del motivo (`denied_tools`, `whitelist`, `unattended`,
  `egress`, `clearance`) e la scrive nella propria telemetria. Da lì arriva a
  `POST /clodia/jobs/refusal/internal`.

Niente euristiche: non si sniffa il testo dei tool-result e non si deduce un
rifiuto dall'assenza di risultato. Un turno che finisce senza aver prodotto nulla
può essere mille cose; un turno in cui qualcuno ha negato un verbo è un fatto, e
si registra solo dove quel fatto viene stabilito.

## Il ciclo di vita è quello del TURNO

Un rifiuto vale per il run dentro cui è avvenuto. Il registro si azzera
all'inizio del run (`_complete_agentic_run`) e si consuma alla fine: senza,
un diniego incassato ieri — o in una chat interattiva sulla stessa sessione —
farebbe fallire il run di oggi, che è l'errore opposto e non meno grave.

In memoria per la stessa ragione di `run_status`: la finestra è interna a un
turno dello stesso processo, e persisterla creerebbe stato da riconciliare al
boot senza rispondere a nessuna domanda in più.
"""
from __future__ import annotations

import threading
from typing import Optional

#: Classe di motivo per i dinieghi decisi da NOI (non dal gateway): il tool non
#: è fra i `native_tools` dichiarati dal seed. Le altre classi arrivano dal
#: gateway, che le calcola già per la propria telemetria.
WHY_NATIVE_TOOLS = "native_tools"

# chat_id → lista di rifiuti, in ordine di accadimento. Lista e non insieme: tre
# tentativi dello stesso verbo sono un fatto diverso da uno, e il riassunto li
# conta (job 4, run 28: `email.send` tre volte).
_RIFIUTI: dict[str, list[dict]] = {}
_LOCK = threading.Lock()

#: Tetto per turno. Un modello che insiste su un verbo negato può farlo a lungo:
#: il registro serve a NOMINARE il verbo, non a contare fino a mille, e la
#: memoria di un turno non deve dipendere dalla testardaggine di chi lo esegue.
MAX_PER_TURN = 50


def note(chat_id: str, verb: str, why: str = "") -> None:
    """Registra UN rifiuto avvenuto nel turno `chat_id`.

    Solleva `ValueError` su chat o verbo vuoti: una riga «verbo negato: ''»
    degraderebbe a rumore il dettaglio del run proprio nel punto in cui deve
    nominare qualcosa. Chi chiama da un percorso di errore (il gateway) non deve
    però pagare l'eccezione — vedi il chiamante, che è best-effort.
    """
    cid = str(chat_id or "").strip()
    if not cid:
        raise ValueError("chat_id richiesto per registrare un rifiuto")
    v = str(verb or "").strip()
    if not v:
        raise ValueError("il nome del verbo negato è richiesto: senza, il "
                         "dettaglio del run non nomina nulla")
    w = str(why or "").strip()
    with _LOCK:
        righe = _RIFIUTI.setdefault(cid, [])
        if len(righe) < MAX_PER_TURN:
            righe.append({"verb": v, "why": w})


def take(chat_id: str) -> list[dict]:
    """Consuma i rifiuti del turno `chat_id`. Lista vuota se non ce ne sono.

    Lista SEMPRE — mai `None`: un chiamante che testasse la verità di un oggetto
    qualunque leggerebbe «c'è stato un rifiuto» su ogni run dei runtime che il
    registro non alimenta, e ogni loro run diventerebbe `error`.
    """
    cid = str(chat_id or "").strip()
    with _LOCK:
        return _RIFIUTI.pop(cid, [])


def peek(chat_id: str) -> list[dict]:
    """I rifiuti pendenti senza consumarli. Per i test e la diagnostica."""
    with _LOCK:
        return list(_RIFIUTI.get(str(chat_id or "").strip(), []))


def forget(chat_id: str) -> None:
    """Azzera il registro del turno. Chiamato all'INIZIO di ogni run: è ciò che
    impedisce a un rifiuto di ieri di descrivere il lavoro di oggi."""
    cid = str(chat_id or "").strip()
    with _LOCK:
        _RIFIUTI.pop(cid, None)


def pending_count() -> int:
    """Quanti turni hanno rifiuti non consumati. Se cresce senza scendere, c'è un
    turno che non passa dal completamento."""
    with _LOCK:
        return len(_RIFIUTI)


def summary(rifiuti: Optional[list[dict]]) -> str:
    """Riassunto leggibile: `email.send ×3 (denied_tools), web.fetch (whitelist)`.

    Aggrega per (verbo, classe) perché tre righe identiche non dicono nulla in
    più di `×3`, e il dettaglio di un run lo legge una persona.
    """
    conteggi: dict[tuple[str, str], int] = {}
    for r in rifiuti or []:
        chiave = (str(r.get("verb") or "").strip(), str(r.get("why") or "").strip())
        if not chiave[0]:
            continue
        conteggi[chiave] = conteggi.get(chiave, 0) + 1
    pezzi = []
    for (verbo, classe), n in conteggi.items():
        testo = verbo if n == 1 else f"{verbo} ×{n}"
        pezzi.append(f"{testo} ({classe})" if classe else testo)
    return ", ".join(pezzi)
