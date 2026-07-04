"""Import di PACK da archivio .zip o da URL (git repo o .zip remoto).

Un pack = [skills] + [rules] + [mcp_servers], nessuno obbligatorio. Formati
riconosciuti (in ordine di precedenza):

1. **Claude plugin** — `.claude-plugin/plugin.json` alla root (o un livello
   sotto, per gli zip che incapsulano una cartella). Skills = ogni cartella
   con SKILL.md; mcp = `mcpServers` in plugin.json oppure `.mcp.json`.
2. **Clodia pack** — `pack.yaml` alla root con `name`, `description`,
   `version`, `mcp_servers`. Skills = cartelle con SKILL.md; rules =
   `rules/*.md`.
3. **Bare skills** — nessun manifest: fallback al comportamento storico,
   tutte le skill trovate finiscono nel pack `user-pack`.

Destinazioni:
- skills  → `CLODIA_DATA/skills-catalog/<pack>/<skill>/`
- rules   → `CLODIA_DATA/rules-catalog/<pack>/<rule>.md`
- manifest (metadata + mcp_servers) → `CLODIA_DATA/packs/<pack>/pack.yaml`

Gli MCP server dichiarati dal pack NON vengono montati automaticamente sul
gateway (Prima Legge: nessun processo/endpoint arbitrario attivato da uno zip
importato): sono esposti dal catalogo e il mount resta un'azione esplicita
dell'owner dalla sezione Tools.

Sicurezza: riusa le guardie di skill_import (zip-slip, limiti dimensione,
clone shallow con timeout).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from ..config import data_path
from . import catalog
from .skill_import import (
    SkillImportError,
    _discover_skill_dirs,
    _download,
    _git_clone,
    _install_from_root,
    _safe_extract_zip,
)

LOG = logging.getLogger("agent-server.api.pack_import")

PACKS_META_DIR = data_path("packs")

# Pack riservati: base-pack è il catalogo logic (git), local-pack è la label
# implicita delle entry flat del data catalog. Nessuno dei due è importabile.
RESERVED_PACK_NAMES = {"base-pack", "local-pack", "logic"}


class PackImportError(SkillImportError):
    """Errore d'import pack gestito (→ 400 lato API)."""


def _sanitize_pack_name(raw: Any) -> str:
    """Normalizza il nome pack (lower, separatori → '-') e lo valida."""
    name = re.sub(r"[^a-z0-9_-]+", "-", str(raw or "").strip().lower()).strip("-")
    if not name or not catalog._NAME_RE.fullmatch(name):
        raise PackImportError(f"nome pack non valido: '{raw}'")
    if name in RESERVED_PACK_NAMES:
        raise PackImportError(f"nome pack riservato: '{name}'")
    return name


def _find_manifest(root: Path) -> tuple[Path, dict[str, Any], str] | None:
    """Cerca un manifest pack alla root o un livello sotto (zip incapsulati).

    Ritorna (pack_root, manifest, kind) con kind in {"plugin", "pack"}."""
    candidates = [root]
    try:
        candidates += sorted(
            c for c in root.iterdir() if c.is_dir() and c.name != ".git"
        )
    except OSError:
        pass
    for cand in candidates:
        plugin_json = cand / ".claude-plugin" / "plugin.json"
        if plugin_json.is_file():
            try:
                manifest = json.loads(plugin_json.read_text(encoding="utf-8"))
            except Exception as e:
                raise PackImportError(f"plugin.json non valido: {str(e)[:120]}")
            if not isinstance(manifest, dict):
                raise PackImportError("plugin.json non valido: atteso un oggetto")
            return cand, manifest, "plugin"
        pack_yaml = cand / "pack.yaml"
        if pack_yaml.is_file():
            try:
                manifest = yaml.safe_load(pack_yaml.read_text(encoding="utf-8")) or {}
            except Exception as e:
                raise PackImportError(f"pack.yaml non valido: {str(e)[:120]}")
            if not isinstance(manifest, dict):
                raise PackImportError("pack.yaml non valido: atteso un mapping")
            return cand, manifest, "pack"
    return None


def _manifest_mcp_servers(pack_root: Path, manifest: dict[str, Any], kind: str) -> dict[str, Any]:
    """Estrae la mappa name→config degli MCP server del pack.

    Claude plugin: `mcpServers` in plugin.json (inline) oppure `.mcp.json`
    alla root del plugin. Clodia pack: `mcp_servers` (o `mcpServers`) in
    pack.yaml."""
    servers: Any = None
    if kind == "plugin":
        servers = manifest.get("mcpServers")
        if servers is None:
            mcp_json = pack_root / ".mcp.json"
            if mcp_json.is_file():
                try:
                    parsed = json.loads(mcp_json.read_text(encoding="utf-8"))
                except Exception as e:
                    raise PackImportError(f".mcp.json non valido: {str(e)[:120]}")
                if isinstance(parsed, dict):
                    servers = parsed.get("mcpServers", parsed)
    else:
        servers = manifest.get("mcp_servers") or manifest.get("mcpServers")
    if servers is None:
        return {}
    if not isinstance(servers, dict):
        raise PackImportError("mcp servers del pack non validi: attesa mappa name→config")
    out: dict[str, Any] = {}
    for name, config in servers.items():
        if not isinstance(config, dict):
            raise PackImportError(f"config MCP '{name}' non valida: atteso un oggetto")
        out[str(name)] = config
    return out


def _install_skill_into_pack(sdir: Path, pack: str) -> str:
    """Copia una cartella skill in `skills-catalog/<pack>/<name>/`."""
    try:
        frontmatter, _b, _f = catalog._read_catalog_file(sdir / "SKILL.md")
    except Exception:
        frontmatter = {}
    name = str(frontmatter.get("name") or sdir.name).strip()
    if not catalog._NAME_RE.fullmatch(name):
        raise PackImportError(
            f"nome skill non valido: '{name}' (minuscole, cifre, - e _)")
    dst = catalog.DATA_SKILLS_DIR / pack / name
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(sdir, dst, ignore=shutil.ignore_patterns(".git"))
    return name


def _install_rule_into_pack(rfile: Path, pack: str) -> str:
    """Copia un file rule in `rules-catalog/<pack>/<name>.md`."""
    name = rfile.stem
    if not catalog._NAME_RE.fullmatch(name):
        raise PackImportError(
            f"nome rule non valido: '{name}' (minuscole, cifre, - e _)")
    dst = catalog.DATA_RULES_DIR / pack / f"{name}.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rfile, dst)
    return name


def _discover_rule_files(pack_root: Path) -> list[Path]:
    """Rule del pack: file `rules/*.md` (README escluso)."""
    rules_dir = pack_root / "rules"
    if not rules_dir.is_dir():
        return []
    return sorted(
        f for f in rules_dir.glob("*.md") if f.name != "README.md"
    )


def _write_pack_manifest(
    pack: str,
    *,
    description: str,
    version: str,
    source: str,
    mcp_servers: dict[str, Any],
) -> None:
    meta_dir = PACKS_META_DIR / pack
    meta_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": pack,
        "description": description,
        "version": version,
        "source": source,
        "origin": "imported",
        "mcp_servers": mcp_servers,
    }
    (meta_dir / "pack.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _install_pack_from_root(root: Path, *, source: str) -> dict[str, Any]:
    """Installa il contenuto di `root` come pack. Ritorna il riepilogo."""
    found = _find_manifest(root)
    if found is None:
        # Nessun manifest: bare skills → user-pack (comportamento storico).
        names = _install_from_root(root)
        return {
            "pack": catalog.USER_PACK,
            "skills": names,
            "rules": [],
            "mcp_servers": [],
        }

    pack_root, manifest, kind = found
    pack = _sanitize_pack_name(manifest.get("name") or pack_root.name)
    description = str(manifest.get("description") or "").strip()
    version = str(manifest.get("version") or "").strip()
    mcp_servers = _manifest_mcp_servers(pack_root, manifest, kind)

    skill_dirs = [
        d for d in _discover_skill_dirs(pack_root)
        if ".claude-plugin" not in d.parts
    ]
    rule_files = _discover_rule_files(pack_root)
    if not skill_dirs and not rule_files and not mcp_servers:
        raise PackImportError(
            "pack vuoto: nessuna skill (SKILL.md), rule (rules/*.md) o MCP server")

    skills = [_install_skill_into_pack(d, pack) for d in skill_dirs]
    rules = [_install_rule_into_pack(f, pack) for f in rule_files]
    _write_pack_manifest(
        pack,
        description=description,
        version=version,
        source=source,
        mcp_servers=mcp_servers,
    )
    catalog._invalidate("skill")
    catalog._invalidate("rule")
    LOG.info(
        "pack '%s' importato (%s): %d skill, %d rule, %d mcp server",
        pack, kind, len(skills), len(rules), len(mcp_servers),
    )
    return {
        "pack": pack,
        "skills": skills,
        "rules": rules,
        "mcp_servers": sorted(mcp_servers),
    }


def import_pack_zip(data: bytes, *, source: str = "zip-upload") -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="clodia-pack-zip-") as tmp:
        root = Path(tmp)
        _safe_extract_zip(data, root)
        return _install_pack_from_root(root, source=source)


def import_pack_url(url: str) -> dict[str, Any]:
    url = (url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise PackImportError("URL non valido (richiesto http/https)")
    with tempfile.TemporaryDirectory(prefix="clodia-pack-url-") as tmp:
        root = Path(tmp)
        if url.lower().endswith(".zip"):
            data = _download(url)
            _safe_extract_zip(data, root)
        else:
            _git_clone(url, root)
        return _install_pack_from_root(root, source=url)
