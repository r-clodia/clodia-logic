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


def effective_tool_permissions(name: str, specs: dict) -> list[str]:
    """Verbi di `name` risolvendo la catena `parents`. `specs` = {nome: spec}."""
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
        for v in (getattr(spec, "tool_permissions", None)
                  or (spec.get("tool_permissions") if isinstance(spec, dict) else None)
                  or []):
            v = str(v).strip()
            if v and v not in fuori:
                fuori.append(v)
        genitori = list(getattr(spec, "parents", None)
                        or (spec.get("parents") if isinstance(spec, dict) else None)
                        or [])
        if chi != ARCHSEED and ARCHSEED not in genitori:
            genitori.append(ARCHSEED)
        for g in genitori:
            coda.append((str(g), prof + 1))
    return fuori


def resolve_for(name: str, specs: Iterable) -> list[str]:
    """Comodità: accetta un iterabile di spec con `.name`."""
    m = {getattr(s, "name", None) or (s.get("name") if isinstance(s, dict) else None): s
         for s in specs}
    m.pop(None, None)
    return effective_tool_permissions(name, m)
