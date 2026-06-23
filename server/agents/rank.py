"""Rango degli agenti — chi parla di default in un canale.

Gerarchia (decisa con owner, 21 giu 2026):
  superadmin-umano > umano > super-AI > AI-normale
A parità di tier conta l'ANZIANITÀ: parla il più anziano (created_at minore).
Es.: Clodia e Ophelia sono entrambe super-AI, ma Clodia è più anziana → Clodia.

Usato dal runtime del canale (Fase 2) per scegliere il risponditore quando non
c'è un @tag esplicito. NON tocca l'autorizzazione (quella è clearance/role).
"""
from __future__ import annotations

from .models import AgentSpec

# tier numerico (più alto = parla prima)
_T_SUPERADMIN_HUMAN = 4
_T_HUMAN = 3
_T_SUPER_AI = 2
_T_AGENT = 1

_LABEL = {
    _T_SUPERADMIN_HUMAN: "superadmin-human",
    _T_HUMAN: "human",
    _T_SUPER_AI: "super-ai",
    _T_AGENT: "agent",
}


def rank_tier(spec: AgentSpec) -> int:
    if spec.type == "human":
        return _T_SUPERADMIN_HUMAN if spec.role == "superadmin" else _T_HUMAN
    if spec.type == "super":
        return _T_SUPER_AI
    return _T_AGENT


def rank_label(spec: AgentSpec) -> str:
    return _LABEL[rank_tier(spec)]


def rank_key(spec: AgentSpec) -> tuple:
    """Chiave di ordinamento: il PRIMO dopo il sort parla. tier desc, poi
    created_at asc (più anziano prima). Senza created_at = meno anziano."""
    return (-rank_tier(spec), spec.created_at or "~")


def highest(specs: list[AgentSpec]) -> AgentSpec | None:
    """L'agente di rango più alto (anzianità come tie-break), o None."""
    ai = [s for s in specs if s.type in ("super", "normal")]
    pool = ai or list(specs)
    return sorted(pool, key=rank_key)[0] if pool else None
