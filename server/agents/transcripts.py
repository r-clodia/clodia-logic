"""Il transcript di un turno, portato fuori dallo spawn prima che sparisca.

clodia-platform#208. Ogni turno del runtime Claude scrive il suo racconto
completo — prompt, testo, ogni tool call — sotto
`spawns/<agent>-<n>/.claude/projects/<slug>/<uuid>.jsonl`. È esattamente quel
che serve a una diagnosi, e viene cancellato col workspace: dal reaper degli
spawn inattivi, dallo sweep di boot, o da un restart del container.

Quel che restava era la metà sbagliata. La history di chat (`sessions/chat-*.jsonl`)
persiste i messaggi ma non le tool call; il log di attività persiste l'esito e
una risposta troncata. La #208 ha misurato il prezzo: un job ha chiuso `success`
per quattro mattine facendo nulla, e la spiegazione della causa era scritta in
chiaro nel transcript del primo run — mai letta, perché la cartella non c'era
più.

Qui non si genera niente di nuovo: si copia un file che esiste già in un posto
che sopravvive. Best-effort per costruzione: nessuna eccezione esce da queste
funzioni, perché un turno non deve morire per una copia diagnostica.

Perché la destinazione è `<agent>/<chat_id>/<uuid>.jsonl` e non
`<agent>/<chat_id>.jsonl` come proponeva la issue: una chat che ottiene un nuovo
spawn (recovery dopo un'eviction, restart) riparte da un file sorgente NUOVO e
più corto. Con un file per chat, la copia successiva sovrascriverebbe proprio i
turni di prima — cioè quelli che si va a cercare quando qualcosa è andato
storto. Un file per sorgente non perde niente, e la vista Jobs elenca una
cartella invece di aprire un file.

Solo il runtime Claude, e non per dimenticanza: `CODEX_HOME` è una home
CONDIVISA (`codex-home/`), fuori dallo spawn e non toccata dallo sweep, e
opencode tiene il suo store per conto proprio. Il buco è dove la #208 l'ha
trovato.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from ..config import data_path

LOG = logging.getLogger("agent-server.agents.transcripts")

TRANSCRIPTS_DIR = data_path("agent-state") / "transcripts"

# Sottocartella dello spawn in cui il runtime Claude tiene i transcript.
_CLAUDE_PROJECTS = (".claude", "projects")

# Un chat_id di canale è `chan:SEAL-1/software-house`: dentro un path
# diventerebbe una gerarchia (o un'uscita, con un `..`). Si tiene un solo
# segmento, e solo caratteri che un nome di file può portare.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(nome: str) -> str:
    pulito = _UNSAFE.sub("_", (nome or "").strip()).strip("._-")
    return pulito[:120] or "senza-nome"


def _sources(spawn_dir) -> list[Path]:
    """I transcript dentro uno spawn. Vuoto se non è un runtime che ne scrive."""
    try:
        root = Path(spawn_dir).joinpath(*_CLAUDE_PROJECTS)
        if not root.is_dir():
            return []
        return sorted(p for p in root.rglob("*.jsonl") if p.is_file())
    except OSError:
        return []


def persist(agent: str, chat_id: str, spawn_dirs) -> list[Path]:
    """Copia i transcript degli spawn indicati sotto `TRANSCRIPTS_DIR`.

    Ritorna i file scritti. Non solleva: su errore logga e ritorna quel che ha
    fatto.
    """
    scritti: list[Path] = []
    dest_dir = TRANSCRIPTS_DIR / _safe(agent) / _safe(chat_id)
    for d in spawn_dirs or []:
        for src in _sources(d):
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / f"{_safe(src.stem)}.jsonl"
                # SHORTCUT: copia integrale a ogni turno. Regge finché il
                #           transcript sta nell'ordine dei MB (misurato: 1,1 MB
                #           dopo una sessione lunga → millisecondi). Sopra, va
                #           fatta incrementale per offset: la sorgente è
                #           append-only, quindi basta la coda oltre la
                #           dimensione già copiata.
                shutil.copyfile(src, dest)
                scritti.append(dest)
            except Exception as e:  # noqa: BLE001 — una copia non ferma un turno
                LOG.warning("transcript non persistito (%s → %s): %s",
                            src, dest_dir, e)
    return scritti


def persist_for(chat) -> list[Path]:
    """`persist` per una sessione di chat.

    La dir di spawn si legge da `spawn_dirs_of`, che è l'UNICO lettore di quel
    dato: le tre classi di sessione lo tengono in due attributi diversi, e la
    lezione già pagata è che due letture della stessa verità sono due posti in
    cui una resta indietro.
    """
    try:
        from ..sdk_runtime.session import spawn_dirs_of
        return persist(getattr(chat, "kind", "") or "sconosciuto",
                       getattr(chat, "chat_id", "") or "senza-chat",
                       spawn_dirs_of(chat))
    except Exception as e:  # noqa: BLE001
        LOG.warning("transcript non persistito per la chat: %s", e)
        return []
