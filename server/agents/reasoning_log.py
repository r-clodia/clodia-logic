"""Storico del RAGIONAMENTO di un turno, per canale (clodia-platform#484).

`thinking_chunk` era un evento e basta: pubblicato sul bus durante lo streaming
e consumato dai soli client connessi in quel momento. Chi riapriva il topic a
turno finito — cioè il caso normale, perché la chat si legge in differita — non
aveva nessun modo di vedere cosa l'agente avesse ragionato. Non era un box
chiuso da espandere: il dato non esisteva più.

**Perché uno store suo e non l'activity log**, che pure esiste già e persiste gli
eventi per agente. Perché l'activity log è indicizzato per AGENTE e *non sa in
che tier sia successo* (`agent-state/activity/<agent>/YYYY-MM-DD.jsonl`): il
ragionamento di un turno in SEAL-4 e quello di un turno in SEAL-0 finirebbero
nello stesso file, e il ragionamento **cita il contenuto del canale**. Qui la
prima cartella del path è il tier, la seconda il canale: il contenimento sta
nella struttura, prima ancora che nella guardia della rotta che lo serve
(`_require_member` in `api/channels.py`).

Formato: `agent-state/reasoning/<tier>/<canale>/YYYY-MM-DD.jsonl`, una riga per
BOLLA — l'aggancio è l'id del messaggio comparso nel canale, perché è la bolla
l'oggetto che si guarda quando si vuole sapere «come c'è arrivato».
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..config import data_path

LOG = logging.getLogger("agent-server.agents.reasoning")

REASONING_DIR = data_path("agent-state") / "reasoning"

#: Tetto per turno: 32k di testa + 32k di coda (decisione dell'owner, #484).
#: Si tengono i DUE capi e non uno solo: la coda è la conclusione — la parte per
#: cui si va a rileggere — e la testa è l'impostazione del problema, senza la
#: quale la conclusione non si capisce. Quello che manca sta in mezzo, ed è
#: dichiarato nel testo invece che taciuto.
TESTA = 32 * 1024
CODA = 32 * 1024

#: Retention, in giorni. Il ragionamento è il dato più voluminoso che la
#: piattaforma scriva per turno: senza scadenza lo store cresce per sempre.
GIORNI = 90

_SEGMENTO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GIORNO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _dir_canale(tier: str, name: str) -> Path:
    """La cartella del canale, rifiutando tutto ciò che potrebbe uscirne.

    `tier` e `name` arrivano dal path di una rotta HTTP: la validazione sta qui,
    nell'unico punto che li trasforma in un percorso, e non in ogni chiamante.
    """
    for pezzo in (tier, name):
        if not _SEGMENTO.match(pezzo or ""):
            raise ValueError(f"segmento di percorso non ammesso: {pezzo!r}")
    return REASONING_DIR / tier / name


def cap(text: str) -> tuple[str, bool]:
    """Il testo entro il tetto, e se è stato tagliato. Vedi `TESTA`/`CODA`."""
    if len(text) <= TESTA + CODA:
        return text, False
    tagliati = len(text) - TESTA - CODA
    return (f"{text[:TESTA]}\n\n[… {tagliati} caratteri di ragionamento omessi …]\n\n"
            f"{text[-CODA:]}"), True


def _file_di_oggi(tier: str, name: str, when: Optional[datetime] = None) -> Path:
    when = when or datetime.now(timezone.utc)
    return _dir_canale(tier, name) / f"{when.strftime('%Y-%m-%d')}.jsonl"


def record(tier: str, name: str, *, message_id: str, spawn: str, seed: str,
           text: str, chat_id: str | None = None) -> None:
    """Associa a una bolla il ragionamento del turno che l'ha prodotta."""
    if not (text or "").strip():
        return
    testo, troncato = cap(text)
    path = _file_di_oggi(tier, name)
    # La scadenza non ha bisogno di un task suo: il trigger naturale è la
    # nascita del file di OGGI, che capita una volta al giorno per canale, e
    # `purge` ripulisce TUTTI i tier — non solo questo.
    # SHORTCUT: la pulizia è agganciata alla scrittura, quindi regge finché la
    #           colonia scrive almeno un ragionamento al giorno (se non scrive,
    #           non cresce nemmeno). Se un giorno servisse la scadenza puntuale
    #           anche a colonia ferma, il posto è `_idle_reaper_loop` in
    #           `main._lifespan`, che gira già ogni 300 s: una chiamata lì, non
    #           un task nuovo.
    primo_del_giorno = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    riga = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "message_id": message_id,
        "spawn": spawn,
        "seed": seed,
        "chat_id": chat_id,
        "chars": len(testo),
        "truncated": troncato,
        "text": testo,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(riga, ensure_ascii=False) + "\n")
    if primo_del_giorno:
        purge()


def _file_recenti(tier: str, name: str) -> list[Path]:
    try:
        cartella = _dir_canale(tier, name)
    except ValueError:
        return []
    if not cartella.is_dir():
        return []
    return sorted(p for p in cartella.glob("*.jsonl") if _GIORNO.match(p.stem))


def _righe(path: Path):
    try:
        contenuto = path.read_text(encoding="utf-8")
    except OSError:
        return
    for riga in contenuto.splitlines():
        riga = riga.strip()
        if not riga:
            continue
        try:
            yield json.loads(riga)
        except json.JSONDecodeError:
            # Una riga mozzata (scrittura interrotta) non deve rendere
            # illeggibile tutto il resto della giornata.
            continue


def index(tier: str, name: str, giorni: int = 30) -> list[str]:
    """Gli id dei messaggi che HANNO un ragionamento salvato.

    È ciò che accende il 💭 sulla bolla in UI: un bottone che apre il vuoto è
    peggio di nessun bottone, quindi la lista è la verità dello store e non una
    congettura del client («è un messaggio di un agente, quindi avrà pensato»).
    """
    out: list[str] = []
    visti: set[str] = set()
    for f in _file_recenti(tier, name)[-giorni:]:
        for riga in _righe(f):
            mid = riga.get("message_id")
            if mid and mid not in visti:
                visti.add(mid)
                out.append(mid)
    return out


def read(tier: str, name: str, message_id: str) -> dict | None:
    """Il ragionamento di UNA bolla, o None. L'ultima scrittura vince."""
    trovato = None
    for f in _file_recenti(tier, name):
        for riga in _righe(f):
            if riga.get("message_id") == message_id:
                trovato = riga
    return trovato


def purge(giorni: int = GIORNI) -> int:
    """Butta i file più vecchi della scadenza, in ogni tier. Torna quanti.

    Si cancella solo ciò che si sa datare: un file il cui nome non è una data
    non viene toccato, perché una pulizia che indovina è un modo di perdere
    dati.
    """
    if not REASONING_DIR.is_dir():
        return 0
    limite = (datetime.now(timezone.utc) - timedelta(days=giorni)).strftime("%Y-%m-%d")
    buttati = 0
    for f in REASONING_DIR.glob("*/*/*.jsonl"):
        if not _GIORNO.match(f.stem) or f.stem >= limite:
            continue
        try:
            f.unlink()
            buttati += 1
        except OSError as e:  # noqa: PERF203 — best effort, un file per volta
            LOG.warning("scadenza ragionamento: %s non rimosso (%s)", f, e)
    return buttati
