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

Gli MCP server dichiarati dal plugin vengono salvati nel manifest. Il layer API
di import, già autorizzato dall'owner/admin, prova poi il mount automatico via
gateway `mcp.add`; se fallisce, il risultato espone un warning strutturato.

In rimozione la directory `plugins/<plugin>/` NON viene cancellata: viene
SPOSTATA in `CLODIA_DATA/plugins-archive/<plugin>-<timestamp>/`, e la risposta
dell'API dice dove è finita (`archive_plugin_dir`). Vale per tutto il contenuto,
non solo per i `datastores:` dichiarati: la dichiarazione è una promessa del pack
developer, la cancellazione è definitiva, e un db creato a runtime non è
dichiarato da nessuno. Si cancella solo ciò che un reinstall rimetterebbe
identico — skill e rule, che stanno altrove.

Sicurezza: riusa le guardie di skill_import (zip-slip, limiti dimensione,
clone shallow con timeout).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import time
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

# Nomi riservati: i pack first-party vivono nel catalogo logic (git), local-pack
# è la label implicita delle entry flat del data catalog. Nessuno è importabile
# dal path terze parti.
RESERVED_PLUGIN_NAMES = {"base-pack", "editorial-pack", "comms-pack", "local-pack", "logic"}


class PluginImportError(SkillImportError):
    """Errore d'import plugin gestito (→ 400 lato API)."""


def _sanitize_plugin_name(raw: Any, *, allow_reserved: bool = False) -> str:
    """Normalizza il nome plugin (lower, separatori → '-') e lo valida."""
    name = re.sub(r"[^a-z0-9_-]+", "-", str(raw or "").strip().lower()).strip("-")
    if not name or not catalog._NAME_RE.fullmatch(name):
        raise PluginImportError(f"nome plugin non valido: '{raw}'")
    # Riservati vietati agli import di terze parti; consentiti al path TRUSTED di
    # update first-party (base-pack contiene un plugin 'base-pack').
    if name in RESERVED_PLUGIN_NAMES and not allow_reserved:
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


def _sanitize_datastores(raw: Any) -> list[dict[str, Any]]:
    """Dichiarazioni datastore del plugin (pack ops): lista di
    {path, purpose?, pii?, backup?}. Path SOLO relativi alla datadir del
    plugin (niente assoluti, niente traversal) — il db resta confinato in
    plugins/<nome>/. Entry malformate vengono scartate, non bloccano l'import."""
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for ds in raw:
        if not isinstance(ds, dict):
            continue
        path = str(ds.get("path") or "").strip()
        if not path or path.startswith(("/", "~")) or ".." in Path(path).parts:
            continue
        out.append({
            "path": path,
            "purpose": str(ds.get("purpose") or ""),
            "pii": bool(ds.get("pii", False)),
            "backup": bool(ds.get("backup", True)),
        })
    return out


def _sanitize_rag_collections(raw: Any) -> list[dict[str, Any]]:
    """Dichiarazioni di COLLECTION RAG del pack (pack ops): lista di
    {name, description?, tier?, resources: [{url|path, doc_name, version, type?, meta?}]}.
    Il corpus/indice NON viaggia nel pack (è infra pgvector); qui c'è il
    *metadato*: nome collection + risorse iniziali da scaricare e indicizzare al
    setup. Il provisioning è dell'agente pack_ops (crea la collection + ingest via
    rag.ingest — idempotente). Entry malformate scartate, non bloccano l'import.

    `path` = file relativo confinato al pack (no assoluti/traversal); `url` = fonte
    da scaricare. Almeno uno dei due per risorsa."""
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for col in raw:
        if not isinstance(col, dict) or not str(col.get("name") or "").strip():
            continue
        resources = []
        for r in (col.get("resources") or []):
            if not isinstance(r, dict):
                continue
            url = str(r.get("url") or "").strip()
            path = str(r.get("path") or "").strip()
            if path and (path.startswith(("/", "~")) or ".." in Path(path).parts):
                path = ""  # scarta path non confinati, tiene eventualmente l'url
            if not (url or path):
                continue
            resources.append({
                "url": url, "path": path,
                "doc_name": str(r.get("doc_name") or "").strip(),
                "version": str(r.get("version") or "").strip(),
                "type": str(r.get("type") or "pdf").strip(),
                "meta": r.get("meta") if isinstance(r.get("meta"), dict) else {},
            })
        out.append({
            "name": str(col["name"]).strip(),
            "description": str(col.get("description") or ""),
            "tier": str(col.get("tier") or "SEAL-0").strip(),
            "resources": resources,
        })
    return out


def _sanitize_requires(raw: Any) -> dict[str, list[str]]:
    """Dipendenze curated del plugin (pack ops): {bin|npm|pip|system: [str]}.
    Solo dichiarazione — l'esecuzione è dell'agente pack_ops (Sysadmin), che
    installa esclusivamente ciò che è dichiarato qui."""
    out: dict[str, list[str]] = {}
    if not isinstance(raw, dict):
        return out
    for tier in ("bin", "npm", "pip", "system"):
        vals = raw.get(tier)
        if isinstance(vals, list):
            clean = [str(v).strip() for v in vals if str(v).strip()]
            if clean:
                out[tier] = clean
    return out


def _sanitize_playbooks(raw: Any) -> dict[str, list[dict[str, str]]]:
    """Playbook per tipo di topic (pack ops UX): {tipo: [{label, skill?}]}.
    Le pills sono GROUNDED: `skill` (formato "<plugin>/<skill>" o nome bare
    base-pack) è il requisito che un agente partecipante deve possedere
    perché la pill appaia. Le label diventano choices della webui: niente
    virgole (separatore del markup) — vengono sostituite."""
    out: dict[str, list[dict[str, str]]] = {}
    if not isinstance(raw, dict):
        return out
    for ttype, pills in raw.items():
        if not isinstance(pills, list):
            continue
        clean: list[dict[str, str]] = []
        for pill in pills:
            if not isinstance(pill, dict):
                continue
            label = str(pill.get("label") or "").strip().replace(",", " –")
            if not label:
                continue
            entry = {"label": label}
            if pill.get("skill"):
                entry["skill"] = str(pill["skill"]).strip()
            clean.append(entry)
        if clean:
            out[str(ttype).strip()] = clean
    return out


def _write_plugin_manifest(
    plugin: str,
    *,
    description: str,
    version: str,
    source: str,
    mcp_servers: dict[str, Any],
    datastores: list[dict[str, Any]] | None = None,
    rag_collections: list[dict[str, Any]] | None = None,
    requires: dict[str, list[str]] | None = None,
    topic_playbooks: dict[str, list[dict[str, str]]] | None = None,
) -> None:
    meta_dir = PLUGINS_META_DIR / plugin
    meta_dir.mkdir(parents=True, exist_ok=True)
    # ${CLAUDE_PLUGIN_ROOT} → path reale nella datadir: la config MCP scritta
    # nel manifest è direttamente montabile (i file mcp/ vengono copiati lì).
    if mcp_servers:
        resolved = json.dumps(mcp_servers).replace(
            "${CLAUDE_PLUGIN_ROOT}", str(meta_dir))
        mcp_servers = json.loads(resolved)
    manifest = {
        "name": plugin,
        "description": description,
        "version": version,
        "source": source,
        "origin": "imported",
        "mcp_servers": mcp_servers,
    }
    if datastores:
        manifest["datastores"] = datastores
    if rag_collections:
        manifest["rag_collections"] = rag_collections
    if requires:
        manifest["requires"] = requires
    if topic_playbooks:
        manifest["topic_playbooks"] = topic_playbooks
    (meta_dir / "plugin.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def install_plugin_from_root(
    root: Path,
    *,
    source: str,
    default_name: str | None = None,
    allow_reserved: bool = False,
) -> dict[str, Any]:
    """Installa il contenuto di `root` come plugin. Ritorna il riepilogo.

    `default_name`: usato dal pack importer per le directory plugin senza
    manifest proprio (il nome viene dal path nel pack, non da user-pack).
    `allow_reserved`: consente nomi riservati (base-pack…) — path trusted di update."""
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

    plugin = _sanitize_plugin_name(manifest.get("name") or plugin_root.name,
                                   allow_reserved=allow_reserved)
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
    datastores = _sanitize_datastores(manifest.get("datastores"))
    rag_collections = _sanitize_rag_collections(manifest.get("rag_collections"))
    requires = _sanitize_requires(manifest.get("requires"))
    topic_playbooks = _sanitize_playbooks(manifest.get("topic_playbooks"))
    _write_plugin_manifest(
        plugin,
        description=description,
        version=version,
        source=source,
        mcp_servers=mcp_servers,
        datastores=datastores,
        rag_collections=rag_collections,
        requires=requires,
        topic_playbooks=topic_playbooks,
    )
    # I file degli MCP server vanno copiati nella datadir (prima restavano nel
    # tmp dell'import → config esposta ma server non montabile). Il server che
    # possiede un datastore deve esistere fisicamente accanto ad esso.
    src_mcp = plugin_root / "mcp"
    if src_mcp.is_dir():
        shutil.copytree(
            src_mcp, PLUGINS_META_DIR / plugin / "mcp",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
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
        "datastores": datastores,
        "requires": requires,
    }


def _archive_root() -> Path:
    """Dove finiscono i dati salvati dalla rimozione.

    Fratello di `plugins/`, non figlio: la directory del plugin è esattamente
    quella che viene cancellata."""
    return PLUGINS_META_DIR.parent / "plugins-archive"


def move_aside(src: Path, archive_root: Path) -> Path:
    """Sposta `src` in `archive_root/<nome>-<timestamp>` invece di cancellarla.

    L'unica primitiva di «rimozione» ammessa su una directory che può contenere
    dati: plugin (datastore e db creati a runtime) e seed agente (memory/ con le
    lesson learned). Solleva `OSError` se lo spostamento non riesce — e in quel
    caso `src` è ancora al suo posto: il chiamante NON deve cancellarla, o
    reintroduce esattamente la perdita che questa funzione evita.
    """
    dest = archive_root / f"{src.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    archive_root.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return dest


def _reinstallable(rel: Path) -> bool:
    """Vero per ciò che un reinstall del pack rimetterebbe identico.

    Il manifest è rigenerato da `_write_plugin_manifest` e `mcp/` è copiato dal
    pack: sono le sole due cose sotto `plugins/<nome>/` di cui esista una
    sorgente. Tutto il resto è comparso a runtime e nessuno lo sa ricreare.
    """
    return rel.parts == ("plugin.yaml",) or rel.parts[:1] == ("mcp",)


def archive_plugin_dir(name: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Sposta VIA l'intera `plugins/<nome>/`, invece di cancellarla.

    Prima si salvava solo ciò che il manifest dichiarava in `datastores:` e il
    resto della directory finiva in un `rmtree`. La dichiarazione però è una
    promessa del pack developer, mentre la cancellazione è definitiva: un db che
    il server MCP si è creato da solo — o un `datastores:` dimenticato — non era
    dichiarato da nessuno e sparivano entrambi in silenzio. Il caso peggiore era
    il manifest SENZA `datastores:`: niente da archiviare, quindi via tutto.

    Ordine di rischio: un archivio con dentro anche due copie di codice
    reinstallabile costa disco, un db cancellato costa il cliente. Quindi si
    sposta tutto e si distingue DOPO, nel report: `archived` sono i datastore
    dichiarati (contratto dell'API, invariato), `undeclared` è ciò che si è
    salvato pur non essendo dichiarato — la riga che dice all'admin che quei
    dati esistevano.

    Ritorna (archiviati, trattenuti). `trattenuti` non vuoto = lo spostamento è
    fallito e la directory NON va cancellata: cancellarla sarebbe esattamente la
    perdita di dati che questa funzione esiste per evitare.
    """
    meta_dir = PLUGINS_META_DIR / name
    if not meta_dir.is_dir():
        return [], []

    declared: list[dict[str, Any]] = []
    manifest = meta_dir / "plugin.yaml"
    if manifest.is_file():
        try:
            meta = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            declared = _sanitize_datastores(
                meta.get("datastores") if isinstance(meta, dict) else None)
        except Exception as e:  # noqa: BLE001
            # Manifest illeggibile: non si sa cosa è dato e cosa è codice — e si
            # archivia comunque tutto, che è la ragione per cui ora si può.
            LOG.warning("manifest di '%s' illeggibile (%s): datastore non "
                        "identificabili, la directory si archivia intera",
                        name, str(e)[:120])

    # Inventario PRIMA dello spostamento: dopo, i path non esistono più.
    contents = [f.relative_to(meta_dir) for f in meta_dir.rglob("*") if f.is_file()]
    declared_paths = [Path(ds["path"]) for ds in declared]

    def _is_declared(rel: Path) -> bool:
        return any(rel == d or d in rel.parents for d in declared_paths)

    undeclared = sorted(str(rel) for rel in contents
                        if not _is_declared(rel) and not _reinstallable(rel))

    try:
        dest_root = move_aside(meta_dir, _archive_root())
    except OSError as e:
        LOG.error("directory del plugin '%s' non archiviabile (%s): resta al suo "
                  "posto, la rimozione non cancella nulla", name, str(e)[:120])
        return [], [{"path": ".", "kept": str(meta_dir), "reason": str(e)[:200]}]

    archived = [{"path": ds["path"], "archived": str(dest_root / ds["path"]),
                 "pii": ds["pii"]}
                for ds in declared if (dest_root / ds["path"]).exists()]
    if undeclared:
        # Non è un errore: è la prova che la regola («rimuovere un pack non
        # cancella dati») non dipende più dalla completezza del manifest.
        LOG.info("plugin '%s': %d file non dichiarati archiviati in %s",
                 name, len(undeclared), dest_root)
        archived.append({"path": ".", "archived": str(dest_root), "pii": None,
                         "undeclared": undeclared})
    return archived, []


def remove_plugin(name: str) -> dict[str, Any]:
    """Rimuove skills/rules/manifest di un plugin, CONSERVANDO i suoi dati.

    Ritorna `{"removed": [path...], "datastores_archived": [...],
    "datastores_retained": [...]}`; `removed` vuoto = plugin inesistente.

    L'unico `rmtree` che resta è su skill e rule: sono copie di ciò che sta nel
    pack, un reinstall le rimette identiche. La directory del plugin non si
    cancella, si sposta (`archive_plugin_dir`) — anche quando il manifest non
    dichiara datastore, perché «non dichiarato» non vuol dire «non è un dato».

    Nessun controllo sui nomi riservati: il chiamante (API) valida prima."""
    meta_dir = PLUGINS_META_DIR / name
    existed = meta_dir.is_dir()
    archived, retained = archive_plugin_dir(name)
    removed: list[str] = []
    if existed and not retained:
        # Spostata, non cancellata: per il chiamante «non è più dov'era» è la
        # stessa cosa, ed è la riga che tiene `removed` vuoto solo quando il
        # plugin non c'era — il segnale con cui l'API risponde 404.
        removed.append(str(meta_dir))
    for t in (catalog.DATA_SKILLS_DIR / name, catalog.DATA_RULES_DIR / name):
        if t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
            removed.append(str(t))
    if removed:
        catalog._invalidate("skill")
        catalog._invalidate("rule")
    return {"removed": removed, "datastores_archived": archived,
            "datastores_retained": retained}


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
