"""Auto-sync delle rules dal catalog centralizzato ai workspace effimeri agent.

Modello speculare a `skill_sync` ma per i rule file Clodia-neutral
(`.agent/rules/<name>.md`). Le rules sono knowledge passivo che il runtime
adapter espone al CLI/SDK agentico selezionato — diverse dalle skill
(workflow attivi invocati per nome) e dal system prompt.

Convenzione file:
- File singoli `.md` (non cartelle come le skill, che hanno asset)
- Frontmatter con `globs:` per il path-based filtering

Due cataloghi per separazione di responsabilità (vedi anche
`skills-catalog/README.md`):

- **logic catalog** (`/clodia/rules-catalog`, in git logic): rules
  universali e distribuibili (es. secrets-handling, git-commit-style,
  python-style).
- **data catalog** (`/datadir/rules-catalog`, in clodia-data di owner):
  rules brand/owner-specific (es. acme-blog-voice,
  acme-next-conventions, agent-server-fastapi).

Precedenza al data catalog: se uno stesso nome esiste in entrambi,
vince il data (override personale).
"""
from __future__ import annotations
import logging
import shutil
from pathlib import Path
from typing import Optional

from ..config import data_path, workspace_path

LOG = logging.getLogger("agent-server.agents.rule_sync")

LOGIC_CATALOG_DIR = workspace_path("catalogs/rules")
DATA_CATALOG_DIR = data_path("rules-catalog")

# Token wildcard = "tutte le rule del catalog" (vedi skill_sync.WILDCARDS).
WILDCARDS = {"*", "**", "**/*"}


def _all_rule_names() -> list[str]:
    """Tutte le rule disponibili (union dei due catalog, data precede logic)."""
    names: list[str] = []
    seen: set[str] = set()
    for catalog in (DATA_CATALOG_DIR, LOGIC_CATALOG_DIR):
        if not catalog.is_dir():
            continue
        for f in sorted(catalog.glob("*.md")):
            if f.stem.upper() == "README":   # doc del catalog, non una rule
                continue
            if f.stem not in seen:
                seen.add(f.stem)
                names.append(f.stem)
    return names


def _resolve_rule_source(name: str) -> Optional[Path]:
    """Trova il file sorgente della rule. Data ha precedenza su logic.

    Cerca `<catalog>/<name>.md` in entrambi i cataloghi.
    """
    for catalog in (DATA_CATALOG_DIR, LOGIC_CATALOG_DIR):
        src = catalog / f"{name}.md"
        if src.is_file():
            return src
    return None


def materialize_rules(
    rules: list[str],
    target_rules_dir: Path,
) -> tuple[int, list[str]]:
    """Materializza le rules dichiarate in `spec.rules` nel target dir.

    Args:
        rules: lista di nomi rule (es. da `spec.rules`)
        target_rules_dir: directory `.agent/rules/` del workspace

    Returns:
        (copied_count, unresolved_names): rules copiate + lista di rules
        non trovate in nessuno dei cataloghi
    """
    if not rules:
        return 0, []

    # Wildcard: `["*"]` (o **, **/*) = tutte le rule del catalog.
    if any(r in WILDCARDS for r in rules):
        rules = _all_rule_names()
        LOG.info("rules wildcard → %d rule dal catalog", len(rules))

    target_rules_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    unresolved: list[str] = []
    for name in rules:
        src = _resolve_rule_source(name)
        if src is None:
            LOG.warning(
                "rule '%s' non risolta (cercato in %s e %s)",
                name, DATA_CATALOG_DIR, LOGIC_CATALOG_DIR,
            )
            unresolved.append(name)
            continue
        dst = target_rules_dir / src.name
        shutil.copy2(src, dst)
        copied += 1
        LOG.debug("materializzata rule '%s' da %s", name, src.parent)
    return copied, unresolved
