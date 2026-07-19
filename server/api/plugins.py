"""API dei PLUGIN: plugin = [skills] + [rules] + [mcp_servers].

Nessun componente è obbligatorio — è lo standard dei plugin di Claude Code.
Il livello superiore è il PACK = [agent seeds] + [plugins] (vedi `packs.py`):
un plugin può vivere anche "sciolto", fuori da qualunque pack.

Sorgenti di enumerazione:
- `base-pack`   → pack bundled first-party (git): `catalogs/packs/base-pack/`
                  (plugin `base-pack` con skill+rule; seed nativi in `agents/`)
- `local-pack`  → entry FLAT del data catalog (skill/rule senza subdir)
- plugin-subdir → `CLODIA_DATA/skills-catalog/<plugin>/` e
                  `CLODIA_DATA/rules-catalog/<plugin>/`
- manifest      → `CLODIA_DATA/plugins/<plugin>/plugin.yaml` (metadata + mcp)

Nota naming: i nomi storici (`base-pack`, `anthropic-pack`, `user-pack`, …)
restano invariati — sono etichette, l'entità che rappresentano è il plugin.

Origine (`origin`): `logic` (base-pack), `local` (local-pack), `external`
(installato al setup da catalogs/external-packs.yaml), `user` (user-pack),
`imported` (importato via zip/URL). Cancellabili solo external/user/imported.

Gli MCP server dei plugin sono ESPOSTI (config con secret mascherati), mai
montati automaticamente sul gateway: il mount è un'azione esplicita dell'owner.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, File, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import workspace_path
from . import catalog, plugin_import
from .plugin_import import RESERVED_PLUGIN_NAMES

LOG = logging.getLogger("agent-server.api.plugins")
router = APIRouter()

EXTERNAL_PACKS_MANIFEST = workspace_path("catalogs/external-packs.yaml")

_TTL_SEC = 30.0
_PLUGINS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}

_SECRET_KEY_RE = re.compile(
    r"(?i)(token|secret|key|password|authorization|bearer|credential)")
_MASK = "•••"

_BUILTIN_DESCRIPTIONS = {
    "base-pack": "Skill e rule native della piattaforma (catalogo logic, in git).",
    "local-pack": "Skill e rule locali dell'istanza (entry flat del data catalog).",
    "user-pack": "Skill importate dall'utente (zip/URL senza manifest di plugin).",
}


def invalidate_plugins() -> None:
    _PLUGINS_CACHE["ts"] = 0.0
    _PLUGINS_CACHE["data"] = None


def _mask_secrets(value: Any, *, key: str = "") -> Any:
    """Maschera i valori stringa sotto chiavi secret-like nella config MCP.

    I placeholder `${VAR}` restano visibili: non sono segreti ma riferimenti
    da risolvere al mount."""
    if isinstance(value, dict):
        return {k: _mask_secrets(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_secrets(v, key=key) for v in value]
    if isinstance(value, str) and _SECRET_KEY_RE.search(key) and "${" not in value:
        return _MASK
    return value


def _external_pack_names() -> set[str]:
    try:
        parsed = yaml.safe_load(EXTERNAL_PACKS_MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if not isinstance(parsed, list):
        return set()
    return {
        str(e.get("pack")) for e in parsed
        if isinstance(e, dict) and e.get("pack")
    }


def _base_pack_license() -> str:
    """Licenza del base-pack, letta dalla sua pack.yaml bundled (fonte unica)."""
    try:
        p = workspace_path("catalogs/packs/base-pack/pack.yaml")
        return str((yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("license") or "").strip()
    except Exception:
        return ""


def _load_plugin_manifest(plugin: str) -> dict[str, Any]:
    path = plugin_import.PLUGINS_META_DIR / plugin / "plugin.yaml"
    if not path.is_file():
        return {}
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        LOG.warning("plugin.yaml di '%s' non leggibile: %s", plugin, e)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _skill_entry(name: str, path: Path) -> dict[str, Any]:
    description = ""
    license_ = ""
    try:
        fm, _body, _full = catalog._read_catalog_file(path)
        description = catalog._skill_description(fm)
        license_ = str(fm.get("license") or "").strip()
    except Exception:
        pass
    # license "" = non dichiarata sulla skill → eredita l'umbrella del pack.
    return {"name": name, "description": description, "license": license_}


def _rule_entry(name: str, path: Path) -> dict[str, Any]:
    try:
        _fm, body, _full = catalog._read_catalog_file(path)
        description = catalog._rule_description(body)
    except Exception:
        description = ""
    return {"name": name, "description": description}


def _collect_plugins() -> dict[str, dict[str, Any]]:
    """name → {skills: [entry], rules: [entry], manifest: dict}."""
    plugins: dict[str, dict[str, Any]] = {}

    def _bucket(name: str) -> dict[str, Any]:
        return plugins.setdefault(name, {"skills": [], "rules": [], "manifest": {}})

    # base-pack: catalogo logic
    for name, path in sorted(catalog._iter_skill_paths(catalog.LOGIC_SKILLS_DIR).items()):
        _bucket("base-pack")["skills"].append(_skill_entry(name, path))
    for name, path in sorted(catalog._iter_rule_paths(catalog.LOGIC_RULES_DIR).items()):
        _bucket("base-pack")["rules"].append(_rule_entry(name, path))

    # data catalog: flat → local-pack, subdir → plugin esplicito
    same_root = (
        catalog.LOGIC_SKILLS_DIR.resolve() == catalog.DATA_SKILLS_DIR.resolve()
        and catalog.LOGIC_RULES_DIR.resolve() == catalog.DATA_RULES_DIR.resolve()
    )
    if not same_root:
        for name, variants in sorted(
                catalog._iter_data_skill_paths(catalog.DATA_SKILLS_DIR).items()):
            for plugin_label, path in variants:
                _bucket(plugin_label or "local-pack")["skills"].append(
                    _skill_entry(name, path))
        for name, variants in sorted(
                catalog._iter_data_rule_paths(catalog.DATA_RULES_DIR).items()):
            for plugin_label, path in variants:
                _bucket(plugin_label or "local-pack")["rules"].append(
                    _rule_entry(name, path))

    # manifest dir: plugin anche solo-MCP (senza skill né rule)
    if plugin_import.PLUGINS_META_DIR.is_dir():
        for child in sorted(plugin_import.PLUGINS_META_DIR.iterdir()):
            if child.is_dir() and (child / "plugin.yaml").is_file():
                _bucket(child.name)["manifest"] = _load_plugin_manifest(child.name)

    for name, bucket in plugins.items():
        if not bucket["manifest"]:
            bucket["manifest"] = _load_plugin_manifest(name)
    return plugins


def _plugin_origin(name: str, external: set[str]) -> str:
    if name == "base-pack":
        return "logic"
    if name == "local-pack":
        return "local"
    if name == catalog.USER_PACK:
        return "user"
    if name in external:
        return "external"
    return "imported"


def _plugin_item(name: str, bucket: dict[str, Any], external: set[str]) -> dict[str, Any]:
    manifest = bucket["manifest"]
    origin = _plugin_origin(name, external)
    mcp_raw = manifest.get("mcp_servers") or {}
    if not isinstance(mcp_raw, dict):
        mcp_raw = {}
    mcp_servers = [
        {
            "name": srv,
            "transport": (config.get("type") or config.get("transport")
                          or ("stdio" if config.get("command") else "http")),
            "config": _mask_secrets(config),
        }
        for srv, config in sorted(mcp_raw.items())
        if isinstance(config, dict)
    ]
    description = str(
        manifest.get("description")
        or _BUILTIN_DESCRIPTIONS.get(name, "")
    ).strip()
    skills = sorted(bucket["skills"], key=lambda e: e["name"])
    rules = sorted(bucket["rules"], key=lambda e: e["name"])
    # Workflow e datastore dichiarati dal plugin (pack ops): esposti così il
    # pack si auto-descrive nella UI accanto a skill/rule/mcp.
    wf_raw = manifest.get("workflows") or {}
    workflows = [
        {
            "name": wname,
            "trigger": wf.get("trigger") or [],
            "stages": [
                {"lane": st.get("lane"), "skill": st.get("skill"),
                 "human_gate": bool(st.get("human_gate"))}
                for st in (wf.get("stages") or []) if isinstance(st, dict)
            ],
        }
        for wname, wf in sorted(wf_raw.items())
        if isinstance(wf, dict)
    ] if isinstance(wf_raw, dict) else []
    ds_raw = manifest.get("datastores") or []
    datastores = [
        {"path": d.get("path"), "purpose": d.get("purpose", ""),
         "pii": bool(d.get("pii")), "backup": bool(d.get("backup", True))}
        for d in ds_raw if isinstance(d, dict) and d.get("path")
    ] if isinstance(ds_raw, list) else []
    return {
        "name": name,
        "description": description,
        "origin": origin,
        "deletable": origin in ("external", "user", "imported"),
        "version": str(manifest.get("version") or "").strip(),
        "source": str(manifest.get("source") or "").strip(),
        # licenza dichiarata del plugin (umbrella per le sue skill); il base-pack
        # la legge dalla propria pack.yaml bundled (first-party AGPL).
        "license": (str(manifest.get("license") or "").strip()
                    or (_base_pack_license() if name == "base-pack" else "")),
        "skills": skills,
        "rules": rules,
        "mcp_servers": mcp_servers,
        "workflows": workflows,
        "datastores": datastores,
        "counts": {
            "skills": len(skills),
            "rules": len(rules),
            "mcp_servers": len(mcp_servers),
            "workflows": len(workflows),
            "datastores": len(datastores),
        },
    }


def list_plugins() -> list[dict[str, Any]]:
    """Lista plugin (cache TTL). Usata anche dall'API packs per il cross-ref."""
    if _PLUGINS_CACHE["data"] is not None and (time.time() - _PLUGINS_CACHE["ts"]) < _TTL_SEC:
        return _PLUGINS_CACHE["data"]
    external = _external_pack_names()
    plugins = _collect_plugins()
    out = [_plugin_item(name, bucket, external) for name, bucket in sorted(plugins.items())]
    # base-pack in testa, poi gli altri in ordine alfabetico
    out.sort(key=lambda p: (p["name"] != "base-pack", p["name"]))
    _PLUGINS_CACHE["data"] = out
    _PLUGINS_CACHE["ts"] = time.time()
    return out


class PluginImportUrl(BaseModel):
    url: str


@router.get("/clodia/plugins")
async def list_plugins_endpoint() -> list[dict[str, Any]]:
    return list_plugins()


@router.get("/clodia/plugins/{name}")
async def get_plugin(name: str):
    if not catalog._NAME_RE.fullmatch(name):
        return JSONResponse(status_code=400, content={"error": "nome non valido"})
    for plugin in list_plugins():
        if plugin["name"] == name:
            return plugin
    return JSONResponse(status_code=404, content={"error": "plugin non trovato"})


@router.post("/clodia/plugins/import")
async def import_plugin_zip(file: UploadFile = File(...)):
    """Importa un plugin da .zip (Claude plugin, plugin.yaml o bare skills)."""
    from .skill_import import SkillImportError
    data = await file.read()
    try:
        result = plugin_import.import_plugin_zip(data, source=file.filename or "zip-upload")
    except SkillImportError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": f"import fallito: {str(e)[:160]}"})
    invalidate_plugins()
    return result


@router.post("/clodia/plugins/import-url")
async def import_plugin_url(payload: PluginImportUrl):
    """Importa un plugin da URL (git repo o .zip remoto)."""
    from .skill_import import SkillImportError
    try:
        result = plugin_import.import_plugin_url(payload.url)
    except SkillImportError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": f"import fallito: {str(e)[:160]}"})
    invalidate_plugins()
    return result


@router.delete("/clodia/plugins/{name}")
async def delete_plugin(name: str):
    """Rimuove un plugin non nativo: skills, rules e manifest.

    Per i plugin external il marker `.external-packs/<pack>.installed` resta al
    suo posto, quindi la rimozione è durevole (nessuna reinstallazione al
    prossimo boot)."""
    if not catalog._NAME_RE.fullmatch(name):
        return JSONResponse(status_code=400, content={"error": "nome non valido"})
    if name in RESERVED_PLUGIN_NAMES:
        return JSONResponse(
            status_code=403, content={"error": f"'{name}' è un plugin nativo, non rimovibile"})
    removed = plugin_import.remove_plugin(name)
    if not removed:
        return JSONResponse(status_code=404, content={"error": "plugin non trovato"})
    invalidate_plugins()
    return {"deleted": name}
