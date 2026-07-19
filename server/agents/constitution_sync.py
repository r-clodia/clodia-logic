"""Risoluzione della costituzione (genoma) di un agent dal catalog.

Modello speculare a `rule_sync`: il campo `constitution: <ref>` di agent.yaml è
risolto da `constitution-catalog/<ref>.md`, con precedenza al data catalog
(override owner-specific) sul logic catalog (genomi distribuiti col bundle).

La costituzione NON è una rule passiva: è il genoma costituzionale dell'agent e
va **fuso in testa al system prompt** al momento della materializzazione del
workspace (vedi workspace.py), così entrambi i motori (claude via system_prompt,
codex via system-prompt.md) la ricevono identica.

Modello di ereditarietà (agent-seed spec): la costituzione è una componente
**innata** del seed — viaggia col genoma, a differenza delle memorie (acquisite).
`constitution: none`/assente = nessuna costituzione (es. worker minimali).
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

from ..config import data_path, workspace_path

LOG = logging.getLogger("agent-server.agents.constitution_sync")

# Le costituzioni sono preamboli al system-prompt dei seed: vivono nel base-pack
# (bundled), come skill/rule/seed nativi.
LOGIC_CATALOG_DIR = workspace_path("catalogs/packs/base-pack/constitutions")
DATA_CATALOG_DIR = data_path("constitution-catalog")

# valori che indicano "nessuna costituzione"
_NONE_VALUES = {None, "", "none", "None"}


def resolve_constitution_source(ref: Optional[str]) -> Optional[Path]:
    """File sorgente della costituzione referenziata. Data ha precedenza su logic.

    Ritorna None se `ref` è vuoto/`none` o se il file non esiste in nessun catalog.
    """
    if ref in _NONE_VALUES:
        return None
    for catalog in (DATA_CATALOG_DIR, LOGIC_CATALOG_DIR):
        src = catalog / f"{ref}.md"
        if src.is_file():
            return src
    LOG.warning(
        "costituzione '%s' non risolta (cercata in %s e %s)",
        ref, DATA_CATALOG_DIR, LOGIC_CATALOG_DIR,
    )
    return None


def load_constitution_text(ref: Optional[str]) -> Optional[str]:
    """Testo della costituzione referenziata, o None se assente/non risolta."""
    src = resolve_constitution_source(ref)
    if src is None:
        return None
    try:
        return src.read_text(encoding="utf-8")
    except OSError as e:  # pragma: no cover
        LOG.warning("lettura costituzione '%s' fallita: %s", ref, e)
        return None
