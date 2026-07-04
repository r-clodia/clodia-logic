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
        return out
    for child in sorted(meta_root.iterdir()):
        manifest_path = child / "pack.yaml"
        if not child.is_dir() or not manifest_path.is_file():
            continue
        manifest = _load_pack_manifest(manifest_path)
        name = child.name
        agent_names = [str(a) for a in (manifest.get("agents") or [])]
        plugin_names = [str(p) for p in (manifest.get("plugins") or [])]
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
            "counts": {
                "agents": len(agents),
                "plugins": len(plugin_children),
            },
        })
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
    return result


@router.delete("/clodia/packs/{name}")
async def delete_pack(name: str):
    """Rimuove un pack: i suoi plugin, i suoi agenti (non nativi) e il manifest."""
    if not catalog._NAME_RE.fullmatch(name):
        return JSONResponse(status_code=400, content={"error": "nome non valido"})
    try:
        result = pack_import.remove_pack(name)
    except KeyError:
        return JSONResponse(status_code=404, content={"error": "pack non trovato"})
    plugins_api.invalidate_plugins()
    return result
