"""Auto-sync delle skill dal catalog centralizzato ai workspace effimeri agent.

Modello: ogni agent dichiara in `agent.yaml.capabilities: [skill_a, skill_b, ...]`
le skill che sa servire. Al boot del workspace effimero (vedi
`workspace.EphemeralWorkspace.create`), per ogni capability:

  1. cerca `<capability>/` prima in DATA_CATALOG (datadir, skill brand-specific
     dell'instance es. `acme-*`), poi in LOGIC_CATALOG (bundle, skill
     generiche/portabili es. `work-on-kanban`, `fact-check`)
  2. copia ricorsivamente in `<workspace>/.agent/skills/<capability>/`

Le skill non risolte sono loggate come warning, ma non bloccano la
creazione del workspace (filosofia: meglio un agente parziale di un
agente non avviabile).

Due cataloghi per chiarezza architetturale:

- **logic catalog** (`/clodia/skills-catalog` nel container, in git logic):
  skill universali e distribuibili a qualunque instance che adotti Clodia.
- **data catalog** (`/datadir/skills-catalog` nel container, in clodia-data
  di owner): skill brand/owner-specific. Non vanno in git logic perché
  non sono pertinenti per altri owner che clonano il bundle.

Precedenza al data catalog: se uno stesso nome esiste in entrambi, vince
il data (override personale). Esempio: owner può sovrascrivere
`fact-check` con una versione che include riferimenti a fonti
confidenziali interne — senza toccare il bundle distribuibile.
"""
from __future__ import annotations
import logging
import shutil
from pathlib import Path
from typing import Optional

from ..config import data_path, workspace_path

LOG = logging.getLogger("agent-server.agents.skill_sync")

LOGIC_CATALOG_DIR = workspace_path("catalogs/skills")
DATA_CATALOG_DIR = data_path("skills-catalog")

# Token wildcard in capabilities/rules/tools = "tutto il catalog". Usato dai
# super-agent (clodia/ophelia) che hanno per definizione l'intero set.
WILDCARDS = {"*", "**", "**/*"}


def _is_skill_dir(p: Path) -> bool:
    return p.is_dir() and (p / "SKILL.md").is_file()


def _resolve_skill_source(cap: str) -> Optional[Path]:
    """Trova la cartella sorgente della skill.

    Supporta:
      - capability QUALIFICATA `<pack>/<skill>` → `DATA/<pack>/<skill>/`
      - capability BARE `<skill>` → data flat, poi pack-subdir data, poi logic.
    Data ha precedenza su logic."""
    if "/" in cap:
        pack, _, skill = cap.partition("/")
        src = DATA_CATALOG_DIR / pack / skill
        return src if _is_skill_dir(src) else None
    # bare: data flat
    flat = DATA_CATALOG_DIR / cap
    if _is_skill_dir(flat):
        return flat
    # bare: dentro un pack-subdir data (primo match in ordine)
    if DATA_CATALOG_DIR.is_dir():
        for packdir in sorted(DATA_CATALOG_DIR.iterdir()):
            if not packdir.is_dir() or packdir.name.startswith("."):
                continue
            cand = packdir / cap
            if _is_skill_dir(cand):
                return cand
    # logic (base-pack)
    src = LOGIC_CATALOG_DIR / cap
    return src if _is_skill_dir(src) else None


def _all_skill_names() -> list[str]:
    """Tutte le skill disponibili (data flat + pack-subdir + logic; data precede,
    dedup per nome con first-wins). Usato per la wildcard dei super-agent."""
    names: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name not in seen:
            seen.add(name)
            names.append(name)

    if DATA_CATALOG_DIR.is_dir():
        for d in sorted(DATA_CATALOG_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if _is_skill_dir(d):
                _add(d.name)  # skill flat (user/local-pack)
            else:
                for gc in sorted(d.iterdir()):  # pack-subdir
                    if _is_skill_dir(gc):
                        _add(gc.name)
    if LOGIC_CATALOG_DIR.is_dir():
        for d in sorted(LOGIC_CATALOG_DIR.iterdir()):
            if _is_skill_dir(d):
                _add(d.name)
    return names


def _pack_skill_names(pack: str) -> list[str]:
    """Espande `<pack>/*` nelle skill di quel pack.
    `base-pack`/`logic` → skill del logic catalog (nomi bare); altri pack →
    sotto-dir del data catalog, qualificate `<pack>/<skill>`."""
    out: list[str] = []
    if pack in ("base-pack", "logic"):
        if LOGIC_CATALOG_DIR.is_dir():
            for d in sorted(LOGIC_CATALOG_DIR.iterdir()):
                if _is_skill_dir(d):
                    out.append(d.name)
        return out
    packdir = DATA_CATALOG_DIR / pack
    if packdir.is_dir():
        for d in sorted(packdir.iterdir()):
            if _is_skill_dir(d):
                out.append(f"{pack}/{d.name}")
    return out


def materialize_capabilities(
    capabilities: list[str],
    target_skills_dir: Path,
) -> tuple[int, list[str]]:
    """Materializza le skill dichiarate come `capabilities` nel target dir.

    Args:
        capabilities: lista di nomi skill (es. da `spec.capabilities`)
        target_skills_dir: directory `.agent/skills/` del workspace, gia' creata

    Returns:
        (copied_count, unresolved_names): numero skill copiate + lista
        di capability non trovate in nessuno dei cataloghi
    """
    if not capabilities:
        return 0, []

    # Wildcard catalogo: `*`, `**`, `**/*` = tutte le skill del catalog.
    if any(c in WILDCARDS for c in capabilities):
        capabilities = _all_skill_names()
        LOG.info("capabilities wildcard → %d skill dal catalog", len(capabilities))
    else:
        # Pack-glob `<pack>/*` → tutte le skill di quel pack (grant granulare a pack).
        expanded: list[str] = []
        for cap in capabilities:
            if cap.endswith("/*"):
                pack = cap[:-2]
                names = _pack_skill_names(pack)
                if names:
                    expanded.extend(names)
                    LOG.info("capability pack-glob '%s' → %d skill", cap, len(names))
                else:
                    LOG.warning("capability pack-glob '%s' → pack vuoto/inesistente", cap)
            else:
                expanded.append(cap)
        capabilities = expanded

    copied = 0
    unresolved: list[str] = []
    for cap in capabilities:
        src = _resolve_skill_source(cap)
        if src is None:
            LOG.warning(
                "capability '%s' non risolta (cercato in %s e %s)",
                cap, DATA_CATALOG_DIR, LOGIC_CATALOG_DIR,
            )
            unresolved.append(cap)
            continue
        # capability qualificata `<pack>/<skill>` → dir runtime `<pack>__<skill>`
        dst = target_skills_dir / cap.replace("/", "__")
        # Idempotenza: se esiste già rimuovo prima per evitare merge sporco
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        copied += 1
        LOG.debug("materializzata skill '%s' da %s", cap, src.parent)
    return copied, unresolved
