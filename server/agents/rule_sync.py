"""Auto-sync delle rules dal catalog centralizzato ai workspace effimeri agent.

Modello speculare a `skill_sync` ma per i rule file Clodia-neutral
(`.agent/rules/<name>.md`). Le rules sono knowledge passivo che il runtime
adapter espone al CLI/SDK agentico selezionato — diverse dalle skill
(workflow attivi invocati per nome) e dal system prompt.

Convenzione file:
- File singoli `.md` (non cartelle come le skill, che hanno asset)
- Frontmatter con `globs:` per il path-based filtering

Layout del data catalog (speculare a skill_sync):
- `<rule>.md`         → rule flat (local/user)
- `<pack>/<rule>.md`  → rule dentro un pack esplicito (pack = <pack>)

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


def _is_rule_file(p: Path) -> bool:
    return p.is_file() and p.suffix == ".md" and p.stem.upper() != "README"


def _all_rule_names() -> list[str]:
    """Tutte le rule disponibili (data flat + pack-subdir + logic; data precede,
    dedup per nome con first-wins). Usato per la wildcard dei super-agent."""
    names: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name not in seen:
            seen.add(name)
            names.append(name)

    if DATA_CATALOG_DIR.is_dir():
        for child in sorted(DATA_CATALOG_DIR.iterdir()):
            if _is_rule_file(child):
                _add(child.stem)  # rule flat (user/local-pack)
            elif child.is_dir() and not child.name.startswith("."):
                for f in sorted(child.glob("*.md")):  # pack-subdir
                    if _is_rule_file(f):
                        _add(f.stem)
    if LOGIC_CATALOG_DIR.is_dir():
        for f in sorted(LOGIC_CATALOG_DIR.glob("*.md")):
            if _is_rule_file(f):
                _add(f.stem)
    return names


def _resolve_rule_source(name: str) -> Optional[Path]:
    """Trova il file sorgente della rule. Data ha precedenza su logic.

    Supporta:
      - rule QUALIFICATA `<pack>/<rule>` → `DATA/<pack>/<rule>.md`
      - rule BARE `<rule>` → data flat, poi pack-subdir data, poi logic.
    """
    if "/" in name:
        pack, _, rule = name.partition("/")
        src = DATA_CATALOG_DIR / pack / f"{rule}.md"
        return src if _is_rule_file(src) else None
    flat = DATA_CATALOG_DIR / f"{name}.md"
    if _is_rule_file(flat):
        return flat
    if DATA_CATALOG_DIR.is_dir():
        for packdir in sorted(DATA_CATALOG_DIR.iterdir()):
            if not packdir.is_dir() or packdir.name.startswith("."):
                continue
            cand = packdir / f"{name}.md"
            if _is_rule_file(cand):
                return cand
    src = LOGIC_CATALOG_DIR / f"{name}.md"
    return src if _is_rule_file(src) else None


def _pack_rule_names(pack: str) -> list[str]:
    """Espande `<pack>/*` nelle rule di quel pack.
    `base-pack`/`logic` → rule del logic catalog (nomi bare); altri pack →
    sotto-dir del data catalog, qualificate `<pack>/<rule>`."""
    out: list[str] = []
    if pack in ("base-pack", "logic"):
        if LOGIC_CATALOG_DIR.is_dir():
            for f in sorted(LOGIC_CATALOG_DIR.glob("*.md")):
                if _is_rule_file(f):
                    out.append(f.stem)
        return out
    packdir = DATA_CATALOG_DIR / pack
    if packdir.is_dir():
        for f in sorted(packdir.glob("*.md")):
            if _is_rule_file(f):
                out.append(f"{pack}/{f.stem}")
    return out


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
    else:
        # Pack-glob `<pack>/*` → tutte le rule di quel pack (grant granulare).
        expanded: list[str] = []
        for name in rules:
            if name.endswith("/*"):
                pack = name[:-2]
                names = _pack_rule_names(pack)
                if names:
                    expanded.extend(names)
                    LOG.info("rule pack-glob '%s' → %d rule", name, len(names))
                else:
                    LOG.warning("rule pack-glob '%s' → pack vuoto/inesistente", name)
            else:
                expanded.append(name)
        rules = expanded

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
        # rule qualificata `<pack>/<rule>` → file runtime `<pack>__<rule>.md`
        dst = target_rules_dir / (f"{name.replace('/', '__')}.md" if "/" in name else src.name)
        shutil.copy2(src, dst)
        copied += 1
        LOG.debug("materializzata rule '%s' da %s", name, src.parent)
    return copied, unresolved
