"""Import di PLUGIN da archivio .zip o da URL (git repo o .zip remoto).

Un plugin = [skills] + [rules] + [mcp_servers], nessuno obbligatorio — è lo
standard dei plugin di Claude Code. (Il livello superiore è il PACK =
[agent seeds] + [plugins], vedi `pack_import.py`.)

Formati riconosciuti (in ordine di precedenza):

1. **Claude plugin** — `.claude-plugin/plugin.json` alla root (o un livello
   sotto, per gli zip che incapsulano una cartella). Skills = ogni cartella
   con SKILL.md; mcp = `mcpServers` in plugin.json oppure `.mcp.json`.
2. **Clodia plugin** — `plugin.yaml` (legacy: `pack.yaml`) alla root con
   `name`, `description`, `version`, `mcp_servers`. Skills = cartelle con
   SKILL.md; rules = `rules/*.md`.
3. **Bare skills** — nessun manifest: fallback al comportamento storico,
   tutte le skill trovate finiscono nel plugin `user-pack`.

Destinazioni:
- skills  → `CLODIA_DATA/skills-catalog/<plugin>/<skill>/`
- rules   → `CLODIA_DATA/rules-catalog/<plugin>/<rule>.md`
- manifest (metadata + mcp_servers) → `CLODIA_DATA/plugins/<plugin>/plugin.yaml`

Gli MCP server dichiarati dal plugin NON vengono montati automaticamente sul
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

LOG = logging.getLogger("agent-server.api.plugin_import")

PLUGINS_META_DIR = data_path("plugins")

# Nomi riservati: base-pack è il catalogo logic (git), local-pack è la label
# implicita delle entry flat del data catalog. Nessuno dei due è importabile.
RESERVED_PLUGIN_NAMES = {"base-pack", "local-pack", "logic"}


class PluginImportError(SkillImportError):
    """Errore d'import plugin gestito (→ 400 lato API)."""


def _sanitize_plugin_name(raw: Any) -> str:
    """Normalizza il nome plugin (lower, separatori → '-') e lo valida."""
    name = re.sub(r"[^a-z0-9_-]+", "-", str(raw or "").strip().lower()).strip("-")
    if not name or not catalog._NAME_RE.fullmatch(name):
        raise PluginImportError(f"nome plugin non valido: '{raw}'")
    if name in RESERVED_PLUGIN_NAMES:
        raise PluginImportError(f"nome plugin riservato: '{name}'")
    return name


def _find_manifest(root: Path) -> tuple[Path, dict[str, Any], str] | None:
    """Cerca un manifest plugin alla root o un livello sotto (zip incapsulati).

    Ritorna (plugin_root, manifest, kind) con kind in {"plugin", "clodia"}."""
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
                raise PluginImportError(f"plugin.json non valido: {str(e)[:120]}")
            if not isinstance(manifest, dict):
                raise PluginImportError("plugin.json non valido: atteso un oggetto")
            return cand, manifest, "plugin"
        for fname in ("plugin.yaml", "pack.yaml"):  # pack.yaml = legacy v6.57
            manifest_yaml = cand / fname
            if manifest_yaml.is_file():
                try:
                    manifest = yaml.safe_load(manifest_yaml.read_text(encoding="utf-8")) or {}
                except Exception as e:
                    raise PluginImportError(f"{fname} non valido: {str(e)[:120]}")
                if not isinstance(manifest, dict):
                    raise PluginImportError(f"{fname} non valido: atteso un mapping")
                return cand, manifest, "clodia"
    return None


def _manifest_mcp_servers(plugin_root: Path, manifest: dict[str, Any], kind: str) -> dict[str, Any]:
    """Estrae la mappa name→config degli MCP server del plugin.

    Claude plugin (e bare dir): `mcpServers` in plugin.json (inline) oppure
    `.mcp.json` alla root del plugin. Clodia plugin: `mcp_servers` (o
    `mcpServers`) in plugin.yaml."""
    servers: Any = None
    if kind in ("plugin", "bare"):
        servers = manifest.get("mcpServers")
        if servers is None:
            mcp_json = plugin_root / ".mcp.json"
            if mcp_json.is_file():
                try:
                    parsed = json.loads(mcp_json.read_text(encoding="utf-8"))
                except Exception as e:
                    raise PluginImportError(f".mcp.json non valido: {str(e)[:120]}")
                if isinstance(parsed, dict):
                    servers = parsed.get("mcpServers", parsed)
    else:
        servers = manifest.get("mcp_servers") or manifest.get("mcpServers")
    if servers is None:
        return {}
    if not isinstance(servers, dict):
        raise PluginImportError("mcp servers del plugin non validi: attesa mappa name→config")
    out: dict[str, Any] = {}
    for name, config in servers.items():
        if not isinstance(config, dict):
            raise PluginImportError(f"config MCP '{name}' non valida: atteso un oggetto")
        out[str(name)] = config
    return out


def _install_skill_into_plugin(sdir: Path, plugin: str) -> str:
    """Copia una cartella skill in `skills-catalog/<plugin>/<name>/`."""
    try:
        frontmatter, _b, _f = catalog._read_catalog_file(sdir / "SKILL.md")
    except Exception:
        frontmatter = {}
    name = str(frontmatter.get("name") or sdir.name).strip()
    if not catalog._NAME_RE.fullmatch(name):
        raise PluginImportError(
            f"nome skill non valido: '{name}' (minuscole, cifre, - e _)")
    dst = catalog.DATA_SKILLS_DIR / plugin / name
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(sdir, dst, ignore=shutil.ignore_patterns(".git"))
    return name


def _install_rule_into_plugin(rfile: Path, plugin: str) -> str:
    """Copia un file rule in `rules-catalog/<plugin>/<name>.md`."""
    name = rfile.stem
    if not catalog._NAME_RE.fullmatch(name):
        raise PluginImportError(
            f"nome rule non valido: '{name}' (minuscole, cifre, - e _)")
    dst = catalog.DATA_RULES_DIR / plugin / f"{name}.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rfile, dst)
    return name


def _discover_rule_files(plugin_root: Path) -> list[Path]:
    """Rule del plugin: file `rules/*.md` (README escluso)."""
    rules_dir = plugin_root / "rules"
    if not rules_dir.is_dir():
        return []
    return sorted(
        f for f in rules_dir.glob("*.md") if f.name != "README.md"
    )


def _write_plugin_manifest(
    plugin: str,
    *,
    description: str,
    version: str,
    source: str,
    mcp_servers: dict[str, Any],
) -> None:
    meta_dir = PLUGINS_META_DIR / plugin
    meta_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": plugin,
        "description": description,
        "version": version,
        "source": source,
        "origin": "imported",
        "mcp_servers": mcp_servers,
    }
    (meta_dir / "plugin.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def install_plugin_from_root(
    root: Path,
    *,
    source: str,
    default_name: str | None = None,
) -> dict[str, Any]:
    """Installa il contenuto di `root` come plugin. Ritorna il riepilogo.

    `default_name`: usato dal pack importer per le directory plugin senza
    manifest proprio (il nome viene dal path nel pack, non da user-pack)."""
    found = _find_manifest(root)
    if found is None:
        if default_name is None:
            # Nessun manifest: bare skills → user-pack (comportamento storico).
            names = _install_from_root(root)
            return {
                "plugin": catalog.USER_PACK,
                "skills": names,
                "rules": [],
                "mcp_servers": [],
            }
        plugin_root, manifest, kind = root, {"name": default_name}, "bare"
    else:
        plugin_root, manifest, kind = found

    plugin = _sanitize_plugin_name(manifest.get("name") or plugin_root.name)
    description = str(manifest.get("description") or "").strip()
    version = str(manifest.get("version") or "").strip()
    mcp_servers = _manifest_mcp_servers(plugin_root, manifest, kind)

    skill_dirs = [
        d for d in _discover_skill_dirs(plugin_root)
        if ".claude-plugin" not in d.parts
    ]
    rule_files = _discover_rule_files(plugin_root)
    if not skill_dirs and not rule_files and not mcp_servers:
        raise PluginImportError(
            "plugin vuoto: nessuna skill (SKILL.md), rule (rules/*.md) o MCP server")

    skills = [_install_skill_into_plugin(d, plugin) for d in skill_dirs]
    rules = [_install_rule_into_plugin(f, plugin) for f in rule_files]
    _write_plugin_manifest(
        plugin,
        description=description,
        version=version,
        source=source,
        mcp_servers=mcp_servers,
    )
    catalog._invalidate("skill")
    catalog._invalidate("rule")
    LOG.info(
        "plugin '%s' importato (%s): %d skill, %d rule, %d mcp server",
        plugin, kind, len(skills), len(rules), len(mcp_servers),
    )
    return {
        "plugin": plugin,
        "skills": skills,
        "rules": rules,
        "mcp_servers": sorted(mcp_servers),
    }


def remove_plugin(name: str) -> list[str]:
    """Rimuove skills/rules/manifest di un plugin. Ritorna i path rimossi.

    Nessun controllo sui nomi riservati: il chiamante (API) valida prima."""
    targets = [
        catalog.DATA_SKILLS_DIR / name,
        catalog.DATA_RULES_DIR / name,
        PLUGINS_META_DIR / name,
    ]
    removed: list[str] = []
    for t in targets:
        if t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
            removed.append(str(t))
    if removed:
        catalog._invalidate("skill")
        catalog._invalidate("rule")
    return removed


def import_plugin_zip(data: bytes, *, source: str = "zip-upload") -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="clodia-plugin-zip-") as tmp:
        root = Path(tmp)
        _safe_extract_zip(data, root)
        return install_plugin_from_root(root, source=source)


def import_plugin_url(url: str) -> dict[str, Any]:
    url = (url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise PluginImportError("URL non valido (richiesto http/https)")
    with tempfile.TemporaryDirectory(prefix="clodia-plugin-url-") as tmp:
        root = Path(tmp)
        if url.lower().endswith(".zip"):
            data = _download(url)
            _safe_extract_zip(data, root)
        else:
            _git_clone(url, root)
        return install_plugin_from_root(root, source=url)
