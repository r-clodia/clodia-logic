"""API dei PACK: il pack è l'entità di primo livello del catalogo.

Un pack = [skills] + [rules] + [mcp_servers], nessun componente obbligatorio —
compatibile con i plugin di Claude Code (skills + mcpServers). La pagina Packs
della webui naviga il catalogo come tree: pack → skills/rules/mcp.

Sorgenti di enumerazione:
- `base-pack`   → catalogo logic (git): `catalogs/skills/` + `catalogs/rules/`
- `local-pack`  → entry FLAT del data catalog (skill/rule senza pack-subdir)
- pack-subdir   → `CLODIA_DATA/skills-catalog/<pack>/` e
                  `CLODIA_DATA/rules-catalog/<pack>/`
- manifest      → `CLODIA_DATA/packs/<pack>/pack.yaml` (metadata + mcp_servers)

Origine (`origin`): `logic` (base-pack), `local` (local-pack), `external`
(installato al setup da catalogs/external-packs.yaml), `user` (user-pack),
`imported` (importato via zip/URL). Cancellabili solo external/user/imported.

Gli MCP server dei pack sono ESPOSTI (config con secret mascherati), mai
montati automaticamente sul gateway: il mount è un'azione esplicita dell'owner.
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import workspace_path
from . import catalog, pack_import
from .pack_import import RESERVED_PACK_NAMES

LOG = logging.getLogger("agent-server.api.packs")
router = APIRouter()

EXTERNAL_PACKS_MANIFEST = workspace_path("catalogs/external-packs.yaml")

_TTL_SEC = 30.0
_PACKS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}

_SECRET_KEY_RE = re.compile(
    r"(?i)(token|secret|key|password|authorization|bearer|credential)")
_MASK = "•••"

_BUILTIN_DESCRIPTIONS = {
    "base-pack": "Skill e rule native della piattaforma (catalogo logic, in git).",
    "local-pack": "Skill e rule locali dell'istanza (entry flat del data catalog).",
    "user-pack": "Skill importate dall'utente (zip/URL senza manifest di pack).",
}


def _invalidate_packs() -> None:
    _PACKS_CACHE["ts"] = 0.0
    _PACKS_CACHE["data"] = None


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


def _load_pack_manifest(pack: str) -> dict[str, Any]:
    path = pack_import.PACKS_META_DIR / pack / "pack.yaml"
    if not path.is_file():
        return {}
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        LOG.warning("pack.yaml di '%s' non leggibile: %s", pack, e)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _skill_entry(name: str, path: Path) -> dict[str, Any]:
    try:
        fm, _body, _full = catalog._read_catalog_file(path)
        description = catalog._skill_description(fm)
    except Exception:
        description = ""
    return {"name": name, "description": description}


def _rule_entry(name: str, path: Path) -> dict[str, Any]:
    try:
        _fm, body, _full = catalog._read_catalog_file(path)
        description = catalog._rule_description(body)
    except Exception:
        description = ""
    return {"name": name, "description": description}


def _collect_packs() -> dict[str, dict[str, Any]]:
    """name → {skills: [entry], rules: [entry], manifest: dict}."""
    packs: dict[str, dict[str, Any]] = {}

    def _bucket(name: str) -> dict[str, Any]:
        return packs.setdefault(name, {"skills": [], "rules": [], "manifest": {}})

    # base-pack: catalogo logic
    for name, path in sorted(catalog._iter_skill_paths(catalog.LOGIC_SKILLS_DIR).items()):
        _bucket("base-pack")["skills"].append(_skill_entry(name, path))
    for name, path in sorted(catalog._iter_rule_paths(catalog.LOGIC_RULES_DIR).items()):
        _bucket("base-pack")["rules"].append(_rule_entry(name, path))

    # data catalog: flat → local-pack, subdir → pack esplicito
    same_root = (
        catalog.LOGIC_SKILLS_DIR.resolve() == catalog.DATA_SKILLS_DIR.resolve()
        and catalog.LOGIC_RULES_DIR.resolve() == catalog.DATA_RULES_DIR.resolve()
    )
    if not same_root:
        for name, variants in sorted(
                catalog._iter_data_skill_paths(catalog.DATA_SKILLS_DIR).items()):
            for pack_label, path in variants:
                _bucket(pack_label or "local-pack")["skills"].append(
                    _skill_entry(name, path))
        for name, variants in sorted(
                catalog._iter_data_rule_paths(catalog.DATA_RULES_DIR).items()):
            for pack_label, path in variants:
                _bucket(pack_label or "local-pack")["rules"].append(
                    _rule_entry(name, path))

    # manifest dir: pack anche solo-MCP (senza skill né rule)
    if pack_import.PACKS_META_DIR.is_dir():
        for child in sorted(pack_import.PACKS_META_DIR.iterdir()):
            if child.is_dir() and (child / "pack.yaml").is_file():
                _bucket(child.name)["manifest"] = _load_pack_manifest(child.name)

    for name, bucket in packs.items():
        if not bucket["manifest"]:
            bucket["manifest"] = _load_pack_manifest(name)
    return packs


def _pack_origin(name: str, external: set[str]) -> str:
    if name == "base-pack":
        return "logic"
    if name == "local-pack":
        return "local"
    if name == catalog.USER_PACK:
        return "user"
    if name in external:
        return "external"
    return "imported"


def _pack_item(name: str, bucket: dict[str, Any], external: set[str]) -> dict[str, Any]:
    manifest = bucket["manifest"]
    origin = _pack_origin(name, external)
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
    return {
        "name": name,
        "description": description,
        "origin": origin,
        "deletable": origin in ("external", "user", "imported"),
        "version": str(manifest.get("version") or "").strip(),
        "source": str(manifest.get("source") or "").strip(),
        "skills": skills,
        "rules": rules,
        "mcp_servers": mcp_servers,
        "counts": {
            "skills": len(skills),
            "rules": len(rules),
            "mcp_servers": len(mcp_servers),
        },
    }


def _list_packs() -> list[dict[str, Any]]:
    if _PACKS_CACHE["data"] is not None and (time.time() - _PACKS_CACHE["ts"]) < _TTL_SEC:
        return _PACKS_CACHE["data"]
    external = _external_pack_names()
    packs = _collect_packs()
    out = [_pack_item(name, bucket, external) for name, bucket in sorted(packs.items())]
    # base-pack in testa, poi gli altri in ordine alfabetico
    out.sort(key=lambda p: (p["name"] != "base-pack", p["name"]))
    _PACKS_CACHE["data"] = out
    _PACKS_CACHE["ts"] = time.time()
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
    """Importa un pack da .zip (Claude plugin, pack.yaml o bare skills)."""
    from .pack_import import import_pack_zip as _import
    from .skill_import import SkillImportError
    data = await file.read()
    try:
        result = _import(data, source=file.filename or "zip-upload")
    except SkillImportError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": f"import fallito: {str(e)[:160]}"})
    _invalidate_packs()
    return result


@router.post("/clodia/packs/import-url")
async def import_pack_url(payload: PackImportUrl):
    """Importa un pack da URL (git repo o .zip remoto)."""
    from .pack_import import import_pack_url as _import
    from .skill_import import SkillImportError
    try:
        result = _import(payload.url)
    except SkillImportError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": f"import fallito: {str(e)[:160]}"})
    _invalidate_packs()
    return result


@router.delete("/clodia/packs/{name}")
async def delete_pack(name: str):
    """Rimuove un pack non nativo: skills, rules e manifest.

    Per i pack external il marker `.external-packs/<pack>.installed` resta al
    suo posto, quindi la rimozione è durevole (nessuna reinstallazione al
    prossimo boot)."""
    if not catalog._NAME_RE.fullmatch(name):
        return JSONResponse(status_code=400, content={"error": "nome non valido"})
    if name in RESERVED_PACK_NAMES:
        return JSONResponse(
            status_code=403, content={"error": f"'{name}' è un pack nativo, non rimovibile"})
    targets = [
        catalog.DATA_SKILLS_DIR / name,
        catalog.DATA_RULES_DIR / name,
        pack_import.PACKS_META_DIR / name,
    ]
    existing = [t for t in targets if t.is_dir()]
    if not existing:
        return JSONResponse(status_code=404, content={"error": "pack non trovato"})
    for t in existing:
        shutil.rmtree(t, ignore_errors=True)
    catalog._invalidate("skill")
    catalog._invalidate("rule")
    _invalidate_packs()
    return {"deleted": name}
