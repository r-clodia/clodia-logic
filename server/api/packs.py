"""API dei PACK: pack = [agent seeds] + [plugins], nessuno obbligatorio.

Gerarchia del catalogo (decisione 4 lug 2026):

    pack   := [agent seeds] + [plugins]
    plugin := [skills] + [rules] + [mcp_servers]     (standard Claude Code)

I plugin possono vivere anche "sciolti" (fuori da qualunque pack): la lista
plugin completa è su `/clodia/plugins`; qui si espongono i pack (aggregati
importati) con i loro agenti e plugin risolti.

Per ogni agente del pack l'API espone lo stato dei `requires_plugins` del suo
agent.yaml: prerequisiti SOFT — plugin mancante → `missing_plugins` (warning
in UI), mai un errore.

L'import è UNIFICATO: `POST /clodia/packs/import[-url]` accetta sia un pack
sia un plugin sciolto (Claude plugin / plugin.yaml / bare skills) e ritorna
`kind: "pack" | "plugin"`.
"""
from __future__ import annotations

import logging
from typing import Any

import yaml
from fastapi import APIRouter, File, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..agents.loader import registry
from . import catalog, pack_import, plugins as plugins_api

LOG = logging.getLogger("agent-server.api.packs")
router = APIRouter()


def _load_pack_manifest(path) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        LOG.warning("pack.yaml %s non leggibile: %s", path, e)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _agent_entry(name: str, installed_plugins: set[str]) -> dict[str, Any]:
    """Stato di un agente del pack: installato? prerequisiti plugin soddisfatti?"""
    spec = registry.get_by_name(name)
    if spec is None:
        return {
            "name": name,
            "installed": False,
            "description": "",
            "requires_plugins": [],
            "missing_plugins": [],
        }
    requires = [
        {"name": r.name, "hard": r.hard} for r in (spec.requires_plugins or [])
    ]
    missing = [r["name"] for r in requires if r["name"] not in installed_plugins]
    return {
        "name": name,
        "installed": True,
        "description": (spec.description or "").strip(),
        "requires_plugins": requires,
        "missing_plugins": missing,
    }


def _list_packs() -> list[dict[str, Any]]:
    plugin_items = {p["name"]: p for p in plugins_api.list_plugins()}
    installed_plugins = set(plugin_items)
    out: list[dict[str, Any]] = []
    meta_root = pack_import.PACKS_META_DIR
    if not meta_root.is_dir():
        meta_root.mkdir(parents=True, exist_ok=True)
    referenced: set[str] = set()
    for child in sorted(meta_root.iterdir()):
        manifest_path = child / "pack.yaml"
        if not child.is_dir() or not manifest_path.is_file():
            continue
        manifest = _load_pack_manifest(manifest_path)
        name = child.name
        agent_names = [str(a) for a in (manifest.get("agents") or [])]
        plugin_names = [str(p) for p in (manifest.get("plugins") or [])]
        referenced.update(plugin_names)
        agents = [_agent_entry(a, installed_plugins) for a in agent_names]
        plugin_children = [
            plugin_items.get(p, {"name": p, "missing": True}) for p in plugin_names
        ]
        out.append({
            "name": name,
            "description": str(manifest.get("description") or "").strip(),
            "version": str(manifest.get("version") or "").strip(),
            "source": str(manifest.get("source") or "").strip(),
            "agents": agents,
            "plugins": plugin_children,
            "virtual": False,
            "counts": {
                "agents": len(agents),
                "plugins": len(plugin_children),
            },
        })
    # Niente plugin sciolti (spec v0.3 §4b.3): ogni plugin senza pack è esposto
    # come pack VIRTUALE omonimo — il tree della webui mostra solo pack.
    for pname, item in plugin_items.items():
        if pname in referenced:
            continue
        out.append({
            "name": pname,
            "description": item.get("description") or "",
            "version": item.get("version") or "",
            "source": item.get("source") or "",
            "agents": [],
            "plugins": [item],
            "virtual": True,
            "counts": {"agents": 0, "plugins": 1},
        })
    out.sort(key=lambda x: (x["name"] != "base-pack", x["name"]))
    return out


class PackImportUrl(BaseModel):
    url: str


@router.get("/clodia/packs")
async def list_packs() -> list[dict[str, Any]]:
    return _list_packs()


@router.get("/clodia/packs/{name}")
async def get_pack(name: str):
    if not catalog._NAME_RE.fullmatch(name):
        return JSONResponse(status_code=400, content={"error": "nome non valido"})
    for pack in _list_packs():
        if pack["name"] == name:
            return pack
    return JSONResponse(status_code=404, content={"error": "pack non trovato"})


def _has_pack_ops_declarations(result: dict) -> bool:
    """True se l'import ha installato plugin con requires:/datastores: (pack ops)."""
    if result.get("kind") == "packs":
        return any(_has_pack_ops_declarations(r) for r in result.get("packs", []))
    return any(p.get("datastores") or p.get("requires")
               for p in result.get("plugins", []) if isinstance(p, dict))


def _maybe_trigger_pack_ops(result: dict) -> None:
    """Post-import: consegna la riconciliazione all'agente pack_ops (fire-and-forget).

    Solo se QUESTO import ha introdotto dichiarazioni — un import di sole
    skill non deve costare un run dell'agente sysadmin."""
    if not _has_pack_ops_declarations(result):
        return
    import asyncio

    from . import pack_ops
    asyncio.create_task(pack_ops.trigger_reconcile("post-import"))
    result["pack_ops"] = {"scheduled": True}


@router.post("/clodia/packs/import")
async def import_pack_zip(file: UploadFile = File(...)):
    """Import unificato da .zip: pack (agents+plugins) o plugin sciolto."""
    from .skill_import import SkillImportError
    data = await file.read()
    try:
        result = pack_import.import_pack_zip(data, source=file.filename or "zip-upload")
    except SkillImportError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": f"import fallito: {str(e)[:160]}"})
    plugins_api.invalidate_plugins()
    _maybe_trigger_pack_ops(result)
    return result


@router.post("/clodia/packs/import-url")
async def import_pack_url(payload: PackImportUrl):
    """Import unificato da URL (git repo o .zip remoto)."""
    from .skill_import import SkillImportError
    try:
        result = pack_import.import_pack_url(payload.url)
    except SkillImportError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": f"import fallito: {str(e)[:160]}"})
    plugins_api.invalidate_plugins()
    _maybe_trigger_pack_ops(result)
    return result


@router.delete("/clodia/packs/{name}")
async def delete_pack(name: str):
    """Rimuove un pack: i suoi plugin, i suoi agenti (non nativi) e il manifest."""
    if not catalog._NAME_RE.fullmatch(name):
        return JSONResponse(status_code=400, content={"error": "nome non valido"})
    try:
        result = pack_import.remove_pack(name)
    except KeyError:
        # pack virtuale (plugin senza manifest): delega alla rimozione plugin
        from .plugin_import import RESERVED_PLUGIN_NAMES, remove_plugin
        if name in RESERVED_PLUGIN_NAMES:
            return JSONResponse(status_code=403,
                                content={"error": f"'{name}' è nativo, non rimovibile"})
        removed = remove_plugin(name)
        if not removed:
            return JSONResponse(status_code=404, content={"error": "pack non trovato"})
        result = {"deleted": name, "plugins": [name], "agents": []}
    plugins_api.invalidate_plugins()
    return result
