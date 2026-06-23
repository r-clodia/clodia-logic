"""Agent registry: scopre, valida ed espone gli agenti definiti come dati
in `clodia-data/agents/<name>/agent.yaml`.

Questo package NON gestisce lo spawn delle sessioni SDK (lo fa
`server/sdk_runtime/session.py`). Si limita a:
- caricare gli agent.yaml dal disco
- validare lo schema (Pydantic)
- esporre la registry in memoria via API REST
- offrire una primitiva di workspace effimero (creazione + cleanup)
  che il runtime userà al momento dello spawn.
"""
from .loader import AgentRegistry, registry
from .models import AgentSpec

__all__ = ["AgentRegistry", "AgentSpec", "registry"]
