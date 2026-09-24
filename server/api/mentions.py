"""Parser delle mention nei messaggi di canale (issue clodia-platform#83, D1;
confine sinistro e convergenza dei due parser: issue clodia-platform#255;
un solo sigillo: issue clodia-platform#391).

Le mention (`@nome`) diventano un campo STRUTTURATO del messaggio
al momento della scrittura (`post_message`), così chi calcola i badge
azionabili interroga una lista di destinatari e non fa regex sul testo raw.

Regole (falsi positivi esclusi per costruzione):
- il testo dentro i fenced code block (```...```) e l'inline code (`...`)
  non produce mention;
- le righe citate (prefisso `>`) non producono mention;
- `$nome` NON è una mention (#391): il `$` appartiene soltanto agli alias del
  composer (`$recap`), che la webui espande prima dell'invio. Fino a #391
  esisteva la «citazione» `$nome`, che da R12 non apriva più nessun turno:
  un secondo tipo di menzione inerte, da insegnare a ogni agente e ambiguo con
  gli alias;
- il sigillo deve trovarsi a un confine di parola "vero": inizio riga,
  whitespace o punteggiatura di apertura — `/log/@nome`, `a@b.it` e simili
  (path, email, log incollati) non contano.

QUESTO FILE VIVE IN DUE COPIE IDENTICHE, di proposito (issue#255):

    clodia-tools/server/topics/mentions.py     ← campo `mentions`, badge, notifiche
    clodia-logic/server/api/mentions.py        ← router: chi prende il turno

I due sono processi separati (il gateway si raggiunge in HTTP,
`CLODIA_TOOLS_MCP_URL`) e qui non si importa `clodia-logic` né viceversa: è la
stessa scelta già fatta per `composition_epoch` in `clodia-logic
server/agents/trifecta_reset.py`. Prima di #255 le due implementazioni erano
DIVERSE, e quella che decideva se un turno parte era la sbagliata: `foo@bar.com`
era letto come una menzione di `bar`, il badge diceva «nessuna menzione» e il
router rispondeva «la menzione diretta non può essere servita» — cioè nessuno
rispondeva al messaggio.

Le due copie vanno tenute allineate: `diff` fra i due path deve essere vuoto, e
`GOLDEN_CASES` viaggia dentro il modulo perché la suite di ENTRAMBI i repository
la esegua sui propri entry point. Se una delle due copie cambia da sola, il
golden fa rosso da quel lato.
"""
from __future__ import annotations

import re

# Stessa forma dei principal/agent name della piattaforma. L'ordinale
# opzionale `#N` indirizza una ISTANZA di un seed multi-spawn (issue#94):
# `@fullstack-dev#2` → mention strutturata "fullstack-dev#2".
#
# Il secondo gruppo opzionale (`.segmento`) è il nome LUNGO
# `namespace.shortname` (Davide, 12 set 2026): un seed derivato con
# `parents:` prende lo stesso shortname del genitore, e il namespace lo
# distingue quando più business ne derivano uno ciascuno
# (`tomato.fullstack-dev` vs `uncommon.fullstack-dev`). Un solo livello di
# punto, non nidificato: `a.b.c` non è un nome valido qui.
_NAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}(?:\.[A-Za-z0-9][A-Za-z0-9_-]{0,63})?"
_ORDINAL = r"(?:#[1-9][0-9]{0,2})?"

# Sigillo valido solo dopo inizio stringa, whitespace o punteggiatura di
# apertura (non dopo lettere, cifre, `/`, `.`, `$` ecc.).
_MENTION_RE = re.compile(
    rf"(?:(?<=^)|(?<=[\s\(\[\{{<,;:'\"]))@(?P<nome>{_NAME}{_ORDINAL})")

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_QUOTED_LINE_RE = re.compile(r"^[ \t]{0,3}>.*$", re.MULTILINE)


def _scan(text: str) -> list[str]:
    """Nomi menzionati con `@`, nell'ordine di apparizione, in minuscolo.

    Non deduplica: lo fa chi calcola l'elenco.
    """
    if not text:
        return []
    clean = _FENCE_RE.sub(" ", text)
    clean = _INLINE_CODE_RE.sub(" ", clean)
    clean = _QUOTED_LINE_RE.sub(" ", clean)
    return [m.group("nome").lower() for m in _MENTION_RE.finditer(clean)]


def extract_mentions(text: str) -> list[str]:
    """Destinatari menzionati nel testo, deduplicati, in minuscolo,
    nell'ordine di prima apparizione. Lista vuota se nessuna mention."""
    out: list[str] = []
    for nome in _scan(text):
        if nome not in out:
            out.append(nome)
    return out


def extract_tags(text: str) -> list[str]:
    """Tag `@` che convocano un turno — dedup, in ordine, righe citate escluse.

    Dal #391 coincide con `extract_mentions`: esiste un solo sigillo, quindi
    ogni menzione è una convocazione. Restano due nomi perché sono due entry
    point con due chiamanti (router e campo strutturato), e il golden li
    misura entrambi.
    """
    return extract_mentions(text)


#: Casi di riferimento delle due copie: `(testo, attesi)`, dove `attesi` è
#: l'esito sia di `extract_mentions` sia di `extract_tags` (#391).
#: La suite di entrambi i repository li esegue — qui su `extract_mentions`
#: (campo strutturato, badge), in `clodia-logic` anche su `_tags`/`_tagged`,
#: che sono gli entry point del router. È il «one shared rule set with a test
#: that exercises both entry points» chiesto da #255.
GOLDEN_CASES: tuple[tuple[str, list[str]], ...] = (
    # ── #255: un indirizzo email non è una menzione ─────────────────────────
    ("scrivi a foo@bar.com", []),
    ("manda a mario.rossi@cmm.it la LOI", []),
    ("la mia mail è davide@tomato.blue", []),
    ("ticket: support@github.com", []),
    ("costo 50@unita", []),
    ("x@clodia.io", []),
    ("log in /var/@web/x", []),
    # ── una menzione vera resta una menzione vera ───────────────────────────
    ("@clodia guarda support@github.com", ["clodia"]),
    ("scrivi a foo@bar.com poi @clodia rivedi", ["clodia"]),
    ("fai tu @fullstack-dev#2", ["fullstack-dev#2"]),
    ("fai tu @fullstack-dev-124", ["fullstack-dev-124"]),
    ("(vedi @davide) e [cc $anna]", ["davide"]),
    ("@Davide poi @mario e ancora @davide", ["davide", "mario"]),
    ("@dev#0", ["dev"]),
    # ── namespace.shortname (12 set 2026): seed derivati con parents: ───────
    ("fai tu @tomato.fullstack-dev", ["tomato.fullstack-dev"]),
    ("@tomato.fullstack-dev#2 vai", ["tomato.fullstack-dev#2"]),
    ("@tomato.fullstack-dev-124 vai", ["tomato.fullstack-dev-124"]),
    # ── codice e citazioni non convocano nessuno ────────────────────────────
    ("```\ncurl -u a@clodia.io\n```", []),
    ("```\n@clodia guarda qui\n```\nfuori dal blocco @anna", ["anna"]),
    ("usa `ssh a@clodia` per entrare", []),
    ("usa `@clodia` come placeholder", []),
    ("> @clodia aveva scritto così\nrispondo io: @luca", ["luca"]),
    ("il letterale $$davide non conta", []),
    # ── #391: `$` è degli alias del composer, non è una menzione ───────────
    ("ciao @davide, senti $mario", ["davide"]),
    ("$mario avvisa, poi @davide decide", ["davide"]),
    ("@davide procedi, $davide per conoscenza", ["davide"]),
    ("$clodia", []),
    ("usa $recap qui", []),
    ("$@clodia", []),
    ("", []),
    ("nessuna menzione qui", []),
)
