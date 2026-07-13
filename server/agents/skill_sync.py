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


def _datastore_map(pack: str) -> dict[str, str]:
    """{key → path assoluto} dei datastore dichiarati nel plugin.yaml del pack.
    key = basename del path dichiarato senza estensione (es. `data/leads.db` →
    `leads`). Il path assoluto è risolto nel runtime corrente:
    `{CLODIA_DATA}/plugins/<pack>/<path dichiarato>`. Così una skill che scrive
    `<DATASTORE:leads>` è portabile: ogni istanza lo risolve al proprio datadir.
    """
    import yaml
    manifest = data_path("plugins") / pack / "plugin.yaml"
    if not manifest.is_file():
        return {}
    try:
        meta = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str] = {}
    base = data_path("plugins") / pack
    for ds in (meta.get("datastores") or []):
        rel = (ds or {}).get("path")
        if not rel or not isinstance(rel, str):
            continue
        key = Path(rel).stem  # 'data/leads.db' → 'leads'
        out[key] = str((base / rel).resolve())
    return out


def _substitute_datastore_tokens(skill_dir: Path, pack: str) -> None:
    """Sostituisce i token `<DATASTORE:key>` nei file .md della skill col path
    assoluto del datastore nel runtime corrente. Token senza datastore
    corrispondente → lasciato invariato (loggato), mai crash."""
    import re
    dsmap = _datastore_map(pack)
    token_re = re.compile(r"<DATASTORE:([A-Za-z0-9_-]+)>")
    for md in skill_dir.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        if "<DATASTORE:" not in text:
            continue

        def _repl(m: "re.Match") -> str:
            key = m.group(1)
            if key in dsmap:
                return dsmap[key]
            LOG.warning("skill %s: token <DATASTORE:%s> senza datastore nel pack %s",
                        skill_dir.name, key, pack)
            return m.group(0)

        new = token_re.sub(_repl, text)
        if new != text:
            md.write_text(new, encoding="utf-8")


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
        # Token `<DATASTORE:key>` → path assoluto del datastore nel runtime
        # (portabilità: la skill non hardcoda path di una macchina specifica).
        if "/" in cap:
            _substitute_datastore_tokens(dst, cap.partition("/")[0])
        copied += 1
        LOG.debug("materializzata skill '%s' da %s", cap, src.parent)
    return copied, unresolved
