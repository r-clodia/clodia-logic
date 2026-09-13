"""`MEMORY.md` del seed, iniettata nel prompt di ogni spawn.

Questo modulo esiste perché la funzione che fa questo lavoro viveva in
`agents/feedback.py` e si chiamava `prompt_section_for_spec`: un nome e una
casa che raccontavano le lesson dal feedback 👍/👎, mentre il codice inietta
**tutto** `MEMORY.md` — le note che l'agente si scrive, i documenti di
`memory.put_document`, e allora anche il blocco delle lesson. Alla rimozione
del feedback (clodia-logic#416) quel modulo è stato cancellato per intero come
diceva l'inventario dell'issue, e con lui sarebbe sparita la memoria
persistente di ogni agente della colonia: nessun errore, nessun test rosso,
solo agenti che da lì in poi non ricordano più niente.

Due differenze rispetto alla versione precedente, entrambe volute:

- **non scrive nulla.** Prima la lettura passava da `_sync_memory`, che
  riscriveva `MEMORY.md` per rigenerare il blocco delle lesson: una lettura con
  effetti collaterali, eseguita a ogni spawn. Senza feedback non c'è più niente
  da sincronizzare, e leggere torna a essere solo leggere.
- **il blocco managed delle lesson viene tolto dal testo iniettato.** Sulle
  istanze già in giro `MEMORY.md` contiene ancora
  `<!-- clodia:feedback-lessons:start -->…<!-- …:end -->`, quasi sempre col solo
  «_Nessuna lesson appresa._» dentro. Togliere qui evita una migrazione che
  dovrebbe riscrivere i file di ogni agente per cancellare tre righe morte.
"""
from __future__ import annotations

from pathlib import Path

from . import registry

_MEMORY_FILE = "MEMORY.md"
_LESSONS_START = "<!-- clodia:feedback-lessons:start -->"
_LESSONS_END = "<!-- clodia:feedback-lessons:end -->"

#: Titolo della sezione nel system prompt. Invariato rispetto a prima del #416:
#: è il testo che gli agenti già in esecuzione si vedono nel proprio prompt.
_TITOLO = "## Memoria persistente del seed"


def _senza_blocco_lesson(body: str) -> str:
    """`MEMORY.md` privata del blocco managed del feedback, se c'è."""
    inizio = body.find(_LESSONS_START)
    if inizio < 0:
        return body
    fine = body.find(_LESSONS_END, inizio)
    if fine < 0:
        # Marcatore di apertura senza chiusura: non si indovina dove finisce il
        # blocco, e tagliare a caso mangerebbe memoria vera. Si lascia com'è.
        return body
    return (body[:inizio].rstrip() + "\n\n" + body[fine + len(_LESSONS_END):].lstrip()).strip()


def _percorso(spec) -> Path | None:
    if spec is None or not getattr(spec, "agent_dir", None):
        return None
    mem_rel = spec.memory.dir if getattr(spec, "memory", None) else "memory/"
    return Path(spec.agent_dir) / mem_rel / _MEMORY_FILE


def prompt_section_for_spec(spec) -> str:
    """La sezione di prompt con la memoria del seed, o stringa vuota.

    Vuota anche quando `MEMORY.md` non esiste o contiene solo il blocco morto
    del feedback: un titolo senza corpo sotto occuperebbe contesto per dire
    all'agente che non ha memoria, cosa che si vede benissimo dal silenzio.
    """
    path = _percorso(spec)
    if path is None or not path.is_file():
        return ""
    body = _senza_blocco_lesson(path.read_text(encoding="utf-8").strip()).strip()
    if not body:
        return ""
    return f"{_TITOLO}\n\n{body}"


def prompt_section(agent: str) -> str:
    """Come sopra, a partire dal nome dell'agente."""
    return prompt_section_for_spec(registry.get_by_name(agent))
