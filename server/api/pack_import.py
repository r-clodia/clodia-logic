"""Import di PACK da archivio .zip o da URL (git repo o .zip remoto).

Un pack = [agent seeds] + [plugins], nessuno obbligatorio. Il plugin (standard
Claude Code) resta installabile anche "sciolto": l'import è UNIFICATO — se
l'archivio non è un pack, viene delegato a `plugin_import`.

Formato pack (formato Clodia, non esiste uno standard Claude a questo livello):

    pack.yaml                 # name, description, version (+ opz. agents/plugins)
    agents/<seed>/agent.yaml  # seed formato Clodia (+ system-prompt.md, memory/, pfp.png)
    plugins/<plugin>/…        # ciascuno un plugin (plugin.json/plugin.yaml o bare)

È riconosciuto come pack un archivio con `pack.yaml` alla root (o un livello
sotto) accompagnato da directory `agents/` (alias `seeds/`) o `plugins/`, o da
chiavi `agents`/`plugins` nel manifest. Un semplice `pack.yaml` con skill
(formato v6.57) resta un manifest di plugin legacy.

È riconosciuta anche una **directory di pack** (es. repo clodia-packs):
`packs/<n>/pack.yaml` alla root o un livello sotto → ogni `packs/<n>/` viene
importato come pack autonomo (`kind: "packs"` nella risposta). Ha precedenza
sul riconoscimento marketplace (un repo può avere entrambi i manifest).

È inoltre riconosciuto come pack un **Claude marketplace** —
`.claude-plugin/marketplace.json` alla root (o un livello sotto), lo standard
con cui Claude Code distribuisce più plugin in un repo (es. clodia-plugins):
nome/descrizione dal marketplace, plugin dalle `source` dichiarate in
`plugins[]`, agent seed dalle directory `agents|seeds/` se presenti (estensione
Clodia: lo standard Claude non ha il concetto di seed).

Install dei seed (decisione 4 lug 2026): l'agente viene installato E
registrato — copia in `CLODIA_DATA/agents/<name>/`, emissione cert PKI
(gli agenti creati a mano senza cert non si autenticano al gateway e vedono
zero tool), `registry.load()`, whitelist sul gateway. PKI e whitelist sono
best-effort: un errore non blocca l'import (l'entrypoint fa `issue-all` a
ogni boot come rete di sicurezza).

I `requires_plugins` dei seed sono SOFT: plugin mancante → warning esposto
dall'API packs, mai un errore.
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

from ..agents.loader import registry
from ..config import data_path
from . import catalog, plugin_import
from .plugin_import import PluginImportError
from .skill_import import (
    SkillImportError,
    _download,
    _git_clone,
    _safe_extract_zip,
)

LOG = logging.getLogger("agent-server.api.pack_import")

PACKS_META_DIR = data_path("packs")

_AGENT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,30}")
# Nomi nativi non installabili da pack (allineato ad agent_registry._NATIVE_AGENTS).
_NATIVE_AGENTS = {"clodia", "ophelia", "messaggero"}

RESERVED_PACK_NAMES = plugin_import.RESERVED_PLUGIN_NAMES


class PackImportError(SkillImportError):
    """Errore d'import pack gestito (→ 400 lato API)."""


def _sanitize_pack_name(raw: Any) -> str:
    name = re.sub(r"[^a-z0-9_-]+", "-", str(raw or "").strip().lower()).strip("-")
    if not name or not catalog._NAME_RE.fullmatch(name):
        raise PackImportError(f"nome pack non valido: '{raw}'")
    if name in RESERVED_PACK_NAMES:
        raise PackImportError(f"nome pack riservato: '{name}'")
    return name


def _seed_dirs(pack_root: Path) -> list[Path]:
    """Directory seed del pack: `agents/<n>/agent.yaml` (alias `seeds/`)."""
    out: list[Path] = []
    for dirname in ("agents", "seeds"):
        base = pack_root / dirname
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "agent.yaml").is_file():
                out.append(child)
    return out


def _plugin_dirs(pack_root: Path) -> list[Path]:
    base = pack_root / "plugins"
    if not base.is_dir():
        return []
    return sorted(c for c in base.iterdir() if c.is_dir() and not c.name.startswith("."))


def find_pack_root(root: Path) -> tuple[Path, dict[str, Any]] | None:
    """Riconosce un PACK: pack.yaml + (dir agents/seeds/plugins o chiavi nel
    manifest). Ritorna (pack_root, manifest) oppure None (→ non è un pack)."""
    candidates = [root]
    try:
        candidates += sorted(
            c for c in root.iterdir() if c.is_dir() and c.name != ".git"
        )
    except OSError:
        pass
    for cand in candidates:
        pack_yaml = cand / "pack.yaml"
        if not pack_yaml.is_file():
            continue
        try:
            manifest = yaml.safe_load(pack_yaml.read_text(encoding="utf-8")) or {}
        except Exception as e:
            raise PackImportError(f"pack.yaml non valido: {str(e)[:120]}")
        if not isinstance(manifest, dict):
            raise PackImportError("pack.yaml non valido: atteso un mapping")
        has_dirs = bool(_seed_dirs(cand) or _plugin_dirs(cand))
        has_keys = "agents" in manifest or "plugins" in manifest or "seeds" in manifest
        if has_dirs or has_keys:
            return cand, manifest
    return None


def find_packs_directory(root: Path) -> list[Path]:
    """Riconosce una DIRECTORY DI PACK (es. repo clodia-packs): una cartella
    `packs/` — alla root o un livello sotto — le cui sottodirectory hanno un
    `pack.yaml` proprio. Ritorna le dir dei singoli pack ([] se non è una
    directory di pack)."""
    candidates = [root]
    try:
        candidates += sorted(
            c for c in root.iterdir() if c.is_dir() and c.name != ".git"
        )
    except OSError:
        pass
    for cand in candidates:
        base = cand / "packs"
        if not base.is_dir():
            continue
        out = sorted(
            c for c in base.iterdir()
            if c.is_dir() and (c / "pack.yaml").is_file()
        )
        if out:
            return out
    return []


def find_marketplace_root(root: Path) -> tuple[Path, dict[str, Any]] | None:
    """Riconosce un Claude marketplace: `.claude-plugin/marketplace.json` alla
    root o un livello sotto (zip GitHub che incapsulano `repo-main/`).

    Ritorna (marketplace_root, manifest) oppure None."""
    candidates = [root]
    try:
        candidates += sorted(
            c for c in root.iterdir() if c.is_dir() and c.name != ".git"
        )
    except OSError:
        pass
    for cand in candidates:
        mp_json = cand / ".claude-plugin" / "marketplace.json"
        if not mp_json.is_file():
            continue
        try:
            manifest = json.loads(mp_json.read_text(encoding="utf-8"))
        except Exception as e:
            raise PackImportError(f"marketplace.json non valido: {str(e)[:120]}")
        if not isinstance(manifest, dict):
            raise PackImportError("marketplace.json non valido: atteso un oggetto")
        return cand, manifest
    return None


def _marketplace_plugin_dirs(mp_root: Path, manifest: dict[str, Any]) -> list[Path]:
    """Directory dei plugin dichiarati in `plugins[].source` del marketplace.

    Ogni source deve risolvere a una directory DENTRO il marketplace (guardia
    path-traversal). Una entry dichiarata ma assente è un errore esplicito,
    non uno skip silenzioso. Senza entry valide, fallback alla scansione di
    `plugins/` (come per i pack)."""
    entries = manifest.get("plugins")
    if not isinstance(entries, list) or not entries:
        return _plugin_dirs(mp_root)
    mp_resolved = mp_root.resolve()
    out: list[Path] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise PackImportError("marketplace.json: entry di plugins[] non valida")
        src = str(entry.get("source") or "").strip()
        name = str(entry.get("name") or src or "?")
        if not src:
            raise PackImportError(f"marketplace.json: plugin '{name}' senza source")
        pdir = (mp_root / src).resolve()
        if pdir != mp_resolved and mp_resolved not in pdir.parents:
            raise PackImportError(
                f"marketplace.json: source non sicura per '{name}': {src}")
        if not pdir.is_dir():
            raise PackImportError(
                f"marketplace.json: source di '{name}' non trovata: {src}")
        out.append(pdir)
    return out


def _install_seed(sdir: Path) -> dict[str, Any]:
    """Installa e registra un agent seed. Ritorna {name, status, detail?}.

    status: installed | exists | error. Sequenza: copia → PKI (best-effort) →
    registry.load() con rollback se lo spec non valida → whitelist gateway
    (best-effort)."""
    try:
        raw = yaml.safe_load((sdir / "agent.yaml").read_text(encoding="utf-8")) or {}
    except Exception as e:
        return {"name": sdir.name, "status": "error",
                "detail": f"agent.yaml illeggibile: {str(e)[:120]}"}
    name = str(raw.get("name") or sdir.name).strip()
    if not _AGENT_NAME_RE.fullmatch(name):
        return {"name": name, "status": "error", "detail": "nome agente non valido"}
    if name in _NATIVE_AGENTS:
        return {"name": name, "status": "error",
                "detail": "nome nativo della piattaforma, non installabile da pack"}

    dest = registry.base_dir / name
    if dest.exists():
        # Non sovrascrivere un agente esistente (stesso principio di
        # init-datadir.sh: l'editing locale non si perde).
        return {"name": name, "status": "exists"}

    shutil.copytree(sdir, dest, ignore=shutil.ignore_patterns(".git"))
    (dest / "memory").mkdir(exist_ok=True)

    registry.load()
    spec = registry.get_by_name(name)
    if spec is None:
        detail = registry.errors().get(name, "spec non valida")
        shutil.rmtree(dest, ignore_errors=True)
        registry.load()
        return {"name": name, "status": "error", "detail": str(detail)[:200]}

    # PKI: senza cert l'agente non si autentica al gateway (zero tool).
    try:
        from ..colony import pki
        pki.issue_agent_identity(name)
    except Exception as e:  # noqa: BLE001 — best-effort, issue-all al boot recupera
        LOG.warning("PKI issue per '%s' fallita (recupero al prossimo boot): %s", name, e)

    # Whitelist gateway: idempotente, best-effort.
    try:
        from . import gateway_admin
        gateway_admin.register_agent(name, spec.tool_permissions or None)
    except Exception as e:  # noqa: BLE001
        LOG.warning("whitelist gateway per '%s' fallita: %s", name, e)

    LOG.info("agent seed '%s' installato e registrato dal pack", name)
    return {"name": name, "status": "installed"}


def install_pack_from_root(root: Path, *, source: str) -> dict[str, Any]:
    """Installa un archivio come PACK (o delega a plugin_import se non lo è)."""
    marketplace = None
    found = find_pack_root(root)
    if found is None:
        # directory di pack (repo clodia-packs): ogni packs/<n>/ è un pack a sé
        pack_dirs = find_packs_directory(root)
        if pack_dirs:
            results = [install_pack_from_root(p, source=source) for p in pack_dirs]
            return {"kind": "packs",
                    "packs": results,
                    "imported": [r.get("pack") or r.get("plugin") for r in results]}
        marketplace = find_marketplace_root(root)
        if marketplace is not None:
            found = marketplace
    if found is None:
        # Niente plugin sciolti (spec v0.3 §4b.3): l'import di un plugin nudo
        # genera un pack WRAPPER omonimo — il pack è sempre il contenitore.
        result = plugin_import.install_plugin_from_root(root, source=source)
        wrapper = result["plugin"]
        meta_dir = PACKS_META_DIR / wrapper
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / "pack.yaml").write_text(
            yaml.safe_dump({
                "name": wrapper,
                "description": "",
                "version": "",
                "source": source,
                "agents": [],
                "plugins": [wrapper],
            }, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return {"kind": "pack", "pack": wrapper, "agents": [],
                "plugins": [result], "wrapped": True}

    pack_root, manifest = found
    pack = _sanitize_pack_name(manifest.get("name") or pack_root.name)
    description = str(manifest.get("description") or "").strip()
    version = str(manifest.get("version") or "").strip()

    if marketplace is not None:
        plugin_dirs = _marketplace_plugin_dirs(pack_root, manifest)
    else:
        plugin_dirs = _plugin_dirs(pack_root)
    seed_dirs = _seed_dirs(pack_root)
    if not plugin_dirs and not seed_dirs:
        raise PackImportError(
            "pack vuoto: nessun agent seed (agents/<n>/agent.yaml) "
            "né plugin (plugins/<n>/)")

    plugins: list[dict[str, Any]] = []
    for pdir in plugin_dirs:
        try:
            plugins.append(plugin_import.install_plugin_from_root(
                pdir, source=source, default_name=pdir.name))
        except PluginImportError as e:
            raise PackImportError(f"plugin '{pdir.name}': {e}")

    agents = [_install_seed(sdir) for sdir in seed_dirs]
    failed = [a for a in agents if a["status"] == "error"]
    if failed and not any(a["status"] in ("installed", "exists") for a in agents) \
            and not plugins:
        raise PackImportError(
            "nessun componente installato: " + "; ".join(
                f"{a['name']}: {a.get('detail', '')}" for a in failed))

    meta_dir = PACKS_META_DIR / pack
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "pack.yaml").write_text(
        yaml.safe_dump({
            "name": pack,
            "description": description,
            "version": version,
            "source": source,
            "agents": [a["name"] for a in agents if a["status"] != "error"],
            "plugins": [p["plugin"] for p in plugins],
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    LOG.info("pack '%s' importato: %d agent, %d plugin", pack, len(agents), len(plugins))
    return {
        "kind": "pack",
        "pack": pack,
        "agents": agents,
        "plugins": plugins,
    }


def remove_pack(name: str) -> dict[str, Any]:
    """Rimuove un pack: i suoi plugin, i suoi agenti (non nativi) e il manifest.

    Ritorna il riepilogo; solleva KeyError se il pack non esiste."""
    meta = PACKS_META_DIR / name / "pack.yaml"
    if not meta.is_file():
        raise KeyError(name)
    try:
        manifest = yaml.safe_load(meta.read_text(encoding="utf-8")) or {}
    except Exception:
        manifest = {}

    removed_plugins: list[str] = []
    for plugin in manifest.get("plugins") or []:
        plugin = str(plugin)
        if plugin in plugin_import.RESERVED_PLUGIN_NAMES:
            continue
        if plugin_import.remove_plugin(plugin):
            removed_plugins.append(plugin)

    removed_agents: list[str] = []
    for agent in manifest.get("agents") or []:
        agent = str(agent)
        if agent in _NATIVE_AGENTS or not _AGENT_NAME_RE.fullmatch(agent):
            continue
        adir = registry.base_dir / agent
        if adir.is_dir():
            shutil.rmtree(adir, ignore_errors=True)
            removed_agents.append(agent)
    if removed_agents:
        registry.load()

    shutil.rmtree(PACKS_META_DIR / name, ignore_errors=True)
    LOG.info("pack '%s' rimosso: %d plugin, %d agenti", name,
             len(removed_plugins), len(removed_agents))
    return {"deleted": name, "plugins": removed_plugins, "agents": removed_agents}


def import_pack_zip(data: bytes, *, source: str = "zip-upload") -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="clodia-pack-zip-") as tmp:
        root = Path(tmp)
        _safe_extract_zip(data, root)
        return install_pack_from_root(root, source=source)


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
        return install_pack_from_root(root, source=url)
