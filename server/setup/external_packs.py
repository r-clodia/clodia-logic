"""Installer dei pack di skill ESTERNI (anthropic-pack, openai-curated-pack, …).

Al setup iniziale clona i repo dichiarati in `catalogs/external-packs.yaml` e
copia le skill nel catalogo DATA sotto `CLODIA_DATA/skills-catalog/<pack>/<skill>/`.
Il pack è dato dal PATH (sottocartella), quindi i `SKILL.md` originali NON
vengono modificati: contenuto e file LICENSE/NOTICES viaggiano intatti.

- **Idempotente**: un marker per pack in `skills-catalog/.external-packs/` evita
  la reinstallazione. `force=True` reinstalla.
- **Tollerante agli errori di rete**: ogni pack è isolato; un fallita non blocca
  gli altri né il boot.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

from ..config import data_path, workspace_path

LOG = logging.getLogger("agent-server.setup.external_packs")

MANIFEST = workspace_path("catalogs/external-packs.yaml")
DATA_SKILLS_DIR = data_path("skills-catalog")
MARKER_DIR = DATA_SKILLS_DIR / ".external-packs"

_CLONE_TIMEOUT = 240  # secondi per `git clone` di un pack


def _load_manifest() -> list[dict]:
    if not MANIFEST.is_file():
        return []
    try:
        data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or []
    except Exception as e:  # noqa: BLE001
        LOG.warning("manifest pack esterni illeggibile (%s): %s", MANIFEST, e)
        return []
    return [e for e in data if isinstance(e, dict) and e.get("pack") and e.get("repo")]


def install_external_packs(force: bool = False,
                           only: list[str] | None = None) -> dict[str, int]:
    """Installa i pack del manifest. Ritorna {pack: n_skill_installate}.
    I pack già installati (marker presente) sono saltati salvo `force`.

    `only` (terraformazione, spec v0.3 §4b.2): None = tutti (storico);
    lista = solo i pack elencati (anche vuota = nessun pack esterno)."""
    result: dict[str, int] = {}
    for entry in _load_manifest():
        pack = str(entry["pack"]).strip()
        if only is not None and pack not in only:
            LOG.info("pack esterno '%s' non nel profilo (skill_packs) — skip", pack)
            continue
        repo = str(entry["repo"]).strip()
        ref = str(entry.get("ref", "main")).strip()
        subdir = str(entry.get("subdir", "")).strip().strip("/")
        marker = MARKER_DIR / f"{pack}.installed"
        if marker.is_file() and not force:
            LOG.debug("pack '%s' già installato — skip", pack)
            continue
        try:
            n = _install_one(pack, repo, ref, subdir)
        except Exception as e:  # noqa: BLE001 — un pack non deve mai bloccare il boot
            LOG.warning("pack '%s' non installato da %s: %s", pack, repo, e)
            continue
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{repo}@{ref}:{subdir}\n", encoding="utf-8")
        result[pack] = n
        LOG.info("pack '%s': installate %d skill da %s (%s)", pack, n, repo, ref)
    return result


def _install_one(pack: str, repo: str, ref: str, subdir: str) -> int:
    """Clona `repo@ref` (shallow) e copia ogni `<skill>/SKILL.md` sotto `subdir`
    in `skills-catalog/<pack>/<skill>/`. Ritorna il numero di skill copiate."""
    dest_pack = DATA_SKILLS_DIR / pack
    with tempfile.TemporaryDirectory(prefix=f"clodia-pack-{pack}-") as tmp:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", ref, repo, tmp],
            check=True, capture_output=True, timeout=_CLONE_TIMEOUT,
        )
        src_root = Path(tmp) / subdir if subdir else Path(tmp)
        if not src_root.is_dir():
            raise FileNotFoundError(f"subdir '{subdir}' assente nel repo {repo}")
        dest_pack.mkdir(parents=True, exist_ok=True)
        count = 0
        for skill_md in sorted(src_root.glob("*/SKILL.md")):
            sdir = skill_md.parent
            name = sdir.name
            if name.startswith("."):
                continue
            dst = dest_pack / name
            if dst.exists():
                shutil.rmtree(dst)
            # copytree porta con sé TUTTO (asset + LICENSE.txt/NOTICES): nessuna
            # modifica ai file originali — il pack è dato dal path, non dal yaml.
            shutil.copytree(sdir, dst, ignore=shutil.ignore_patterns(".git"))
            count += 1
        return count
