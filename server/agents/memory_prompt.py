"""La `MEMORY.md` del seed, come sezione del prompt.

Questo modulo è nato dallo smontaggio del feedback 👍/👎 (clodia-platform#416).
La funzione che inietta la memoria viveva in `agents/feedback.py` — l'issue la
dava per «codice morto collaterale», ma è l'unico punto che porta la `MEMORY.md`
di un seed dentro `system-prompt.md`: non solo le lesson, anche il blocco dei
documenti scritto da `memory.put_document`. Rimuoverla con il resto avrebbe
tolto a ogni agente la memoria persistente senza che nulla diventasse rosso.

Differenza rispetto a prima, ed è voluta: qui si **legge soltanto**. La vecchia
strada riscriveva `MEMORY.md` a ogni materializzazione di workspace per tenere
aggiornato il blocco gestito delle lesson — un effetto collaterale su disco a
ogni spawn, che senza quel blocco non ha più ragione di esistere. Ed è anche il
motivo per cui il blocco vuoto compariva in TUTTE le memorie mentre nessun
`feedback-lessons.json` esisteva da nessuna parte: lo scriveva la via del
prompt, non un feedback ricevuto.
"""
from __future__ import annotations

import re
from pathlib import Path

_MEMORY_FILE = "MEMORY.md"
_TITOLO = "## Memoria persistente del seed"

#: Il blocco gestito che il feedback teneva aggiornato dentro `MEMORY.md`.
#: Le memorie già sul disco lo contengono (vuoto) e continueranno a contenerlo
#: finché qualcuno non le riscrive: qui lo si toglie in LETTURA, perché portarlo
#: nel prompt significherebbe annunciare a ogni agente una sezione «Lesson
#: learned dal feedback umano» di un meccanismo che non esiste più.
#: SHORTCUT: nessuna migrazione sui file. Regge finché il residuo è il blocco
#:           vuoto lasciato da #416; se un giorno quelle righe portassero dati
#:           veri, andrebbero riscritte una volta sola, non filtrate a ogni let-
#:           tura.
_BLOCCO_LESSON = re.compile(
    r"<!--\s*clodia:feedback-lessons:start\s*-->.*?"
    r"<!--\s*clodia:feedback-lessons:end\s*-->",
    re.DOTALL,
)


def _memory_path(spec) -> Path | None:
    agent_dir = getattr(spec, "agent_dir", None) if spec is not None else None
    if not agent_dir:
        return None
    mem_rel = spec.memory.dir if getattr(spec, "memory", None) else "memory/"
    return Path(agent_dir) / mem_rel / _MEMORY_FILE


def strip_managed_blocks(body: str) -> str:
    """Il testo della memoria senza i blocchi gestiti da meccanismi rimossi."""
    return _BLOCCO_LESSON.sub("", body).strip()


def prompt_section_for_spec(spec) -> str:
    """La sezione di prompt con la memoria persistente del seed, o "" se non c'è.

    Nessuna scrittura: un workspace che si materializza non deve toccare la
    memoria dell'agente per poterla leggere.
    """
    path = _memory_path(spec)
    if path is None or not path.is_file():
        return ""
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    body = strip_managed_blocks(body)
    if not body:
        return ""
    return f"{_TITOLO}\n\n{body}"
