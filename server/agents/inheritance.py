"""Verbi EFFETTIVI di un seed: i propri più quelli ereditati.

Un posto solo, in questo servizio, e la ragione è la stessa che l'ha imposto nel
gateway: la matrice era letta in **tre** punti — la trifecta, il routing dei
responder, il registry — e nessuno risolveva `parents`.

Finché nessun seed ereditava davvero, leggere la dichiarazione equivaleva a
leggere l'effettivo. Dall'8 ago 2026 non più, e la prima conseguenza si è vista
subito: ripulendo i seed dai verbi ridondanti, il punteggio trifecta di
`segretario` è **sceso da 2 a 0**. Un segnale di sicurezza che si abbassa perché
un file è diventato più pulito è la forma peggiore di errore silenzioso — dice
«meno rischioso» dove non è cambiato nulla.

**Questa è una seconda implementazione della stessa regola**, e va detto invece
di scoprirlo fra sei mesi. Il gateway la applica alla propria config per
autorizzare a runtime; qui si applica ai seed per analizzarli. Le fonti sono
diverse, la regola è la stessa, e due copie della stessa regola divergono. Il
giorno in cui una delle due cambia, l'altra va cambiata con lei — e un test del
base-pack confronta i due esiti proprio per accorgersene.
"""
from __future__ import annotations

import logging
from typing import Iterable

LOG = logging.getLogger("agent-server.agents.inheritance")

#: L'arciseed è antenato di TUTTI e non va dichiarato per esserlo: se la sua
#: presenza dipendesse dalla dichiarazione, un seed potrebbe uscire dal modello
#: omettendola, e nessuno lo vedrebbe. La dichiarazione nel file serve a dire la
#: verità a chi legge, non a produrla.
ARCHSEED = "archseed"

_MAX_ANCESTRY = 8


def _campo(spec, nome: str) -> list:
    """Il campo `nome` di una spec, letta per attributo o per chiave."""
    return list(getattr(spec, nome, None)
                or (spec.get(nome) if isinstance(spec, dict) else None)
                or [])


def _ereditato(name: str, specs: dict, campo: str) -> list[str]:
    """Union di `campo` lungo la catena `parents`, dal seed ai suoi antenati.

    UNA camminata per tutti i campi ereditabili, e non per risparmiare righe:
    verbi e skill avevano due risoluzioni diverse — questa, e un'union a un
    livello solo scritta a mano in `EphemeralWorkspace` — e la seconda perdeva i
    nonni senza dirlo (clodia-platform#496). Due copie della stessa regola
    divergono, ed era già la tesi del docstring in testa a questo modulo: la
    differenza è che adesso la copia divergente non c'è più.

    L'ordine è «prima i propri, poi gli antenati in ampiezza»: è l'ordine in cui
    legge chi materializza le skill, e invertirlo farebbe vincere l'antenato
    dove il figlio ha detto qualcosa di suo.
    """
    visti: set = set()
    fuori: list[str] = []
    coda = [(str(name or ""), 0)]
    while coda:
        chi, prof = coda.pop(0)
        if not chi or chi in visti:
            continue
        if prof > _MAX_ANCESTRY:
            LOG.warning("catena `parents` troppo profonda a '%s': troncata", chi)
            continue
        visti.add(chi)
        spec = specs.get(chi)
        if spec is None:
            continue
        for v in _campo(spec, campo):
            v = str(v).strip()
            if v and v not in fuori:
                fuori.append(v)
        genitori = _campo(spec, "parents")
        if chi != ARCHSEED and ARCHSEED not in genitori:
            genitori.append(ARCHSEED)
        for g in genitori:
            coda.append((str(g), prof + 1))
    return fuori


def effective_tool_permissions(name: str, specs: dict) -> list[str]:
    """Verbi di `name` risolvendo la catena `parents`. `specs` = {nome: spec}."""
    return _ereditato(name, specs, "tool_permissions")


def effective_capabilities(name: str, specs: dict) -> list[str]:
    """Skill di `name` risolvendo la catena `parents`. `specs` = {nome: spec}.

    Decisione dell'owner del 2 ott 2026: un seed che discende da un altro ne
    eredita TUTTE le skill. Prima `parents` concedeva solo verbi, e un seed
    derivato nasceva senza mestiere — `tomato.officer` con `capabilities: []`
    non materializzava niente di `officer`.

    **Le wildcard restano wildcard.** `anthropic-pack/*` non si espande qui:
    la espande `skill_sync.materialize_capabilities` leggendo il catalog nel
    momento in cui copia. Espanderla in questa funzione vorrebbe dire
    fotografare il catalog al momento della risoluzione e consegnare al figlio
    una lista che invecchia — col padre che riceve una skill nuova dal pack e il
    figlio no.

    Le `rules` NON passano di qui, ed è una scelta: una rule è scritta per un
    mestiere e un figlio che la eredita se la porta dove non vale (il caso
    `topic-state-boundary` nel seed di ophelia). Se un giorno si decide
    diversamente, il posto è questo — `_ereditato(name, specs, "rules")` — e si
    vedrà che è stato deciso.
    """
    return _ereditato(name, specs, "capabilities")


def _per_nome(specs: Iterable) -> dict:
    m = {getattr(s, "name", None) or (s.get("name") if isinstance(s, dict) else None): s
         for s in specs}
    m.pop(None, None)
    return m


def resolve_for(name: str, specs: Iterable) -> list[str]:
    """Comodità: accetta un iterabile di spec con `.name`."""
    return effective_tool_permissions(name, _per_nome(specs))


def capabilities_for(name: str, specs: Iterable) -> list[str]:
    """Come `resolve_for`, per le skill."""
    return effective_capabilities(name, _per_nome(specs))


def effective_capabilities_of(spec) -> list[str]:
    """Skill effettive di una spec, risolte contro il registry caricato.

    È la forma che serve ai chiamanti veri — lo spawn, le pill, il profilo di
    routing, la scheda dell'agente — e sta qui invece che in ognuno di loro
    perché erano cinque letture della stessa cosa e una sola risolveva i
    `parents`. Chi ha già la mappa delle spec usi `effective_capabilities`:
    questa la va a prendere.

    Import locale del loader: `loader` costruisce le spec e questo modulo le
    legge, quindi importarlo in testa chiuderebbe il cerchio.
    """
    nome = getattr(spec, "name", None) or (spec.get("name") if isinstance(spec, dict) else None)
    if not nome:
        return []
    from .loader import registry
    specs = {s.name: s for s in registry.list() if getattr(s, "name", None)}
    # La spec passata VINCE su quella del registry: un override di scope o una
    # spec appena modificata non devono essere riscritte da una copia vecchia.
    specs[str(nome)] = spec
    return effective_capabilities(str(nome), specs)
