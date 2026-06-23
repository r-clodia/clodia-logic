"""Readonly catalog endpoints for Clodia skills and rules.

Skills and rules live in two catalog roots:
- logic catalog under the bundle workspace (/clodia in Docker)
- data catalog under CLODIA_DATA (/datadir in Docker)

Data entries override logic entries at runtime. The API mirrors that model by
deduplicating on name and exposing `source="both"` when both roots contain the
same item, while returning the data file as the effective body/path.

`source` remains a filesystem-origin field for compatibility. The user-facing
taxonomy is `pack`:
- logic-only entries are `base-pack`
- data entries overriding base entries are `local-pack` unless frontmatter
  explicitly declares a pack
- data-only entries may declare `pack`/`pack_id`; otherwise they are `local-pack`
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..agents.rule_sync import DATA_CATALOG_DIR as DATA_RULES_DIR
from ..agents.rule_sync import LOGIC_CATALOG_DIR as LOGIC_RULES_DIR
from ..agents.skill_sync import DATA_CATALOG_DIR as DATA_SKILLS_DIR
from ..agents.skill_sync import LOGIC_CATALOG_DIR as LOGIC_SKILLS_DIR

LOG = logging.getLogger("agent-server.api.catalog")
router = APIRouter()

CatalogKind = Literal["skill", "rule"]
CatalogSource = Literal["logic", "data", "both"]
CatalogOrigin = Literal["logic", "data"]

_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
_TTL_SEC = 60.0
_LIST_CACHE: dict[CatalogKind, dict[str, Any]] = {
    "skill": {"ts": 0.0, "data": None},
    "rule": {"ts": 0.0, "data": None},
}
_DETAIL_CACHE: dict[CatalogKind, dict[str, dict[str, Any]]] = {
    "skill": {},
    "rule": {},
}


def _cache_fresh(ts: float) -> bool:
    return (time.time() - ts) < _TTL_SEC


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return parsed YAML frontmatter and the body after it."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            raw = "\n".join(lines[1:idx])
            try:
                parsed = yaml.safe_load(raw) or {}
            except Exception as e:
                LOG.warning("frontmatter parse failed: %s", e)
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}
            return parsed, "\n".join(lines[idx + 1:]).lstrip("\n")
    return {}, text


def _first_nonempty_line(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    for line in value.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _skill_description(frontmatter: dict[str, Any]) -> str:
    return _first_nonempty_line(frontmatter.get("description"))


def _rule_description(body: str) -> str:
    h1_fallback = ""
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if not h1_fallback:
                h1 = line.lstrip("#").strip()
                h1_fallback = re.sub(r"^Rule:\s*", "", h1, flags=re.IGNORECASE)
            continue
        return line
    return h1_fallback


def _frontmatter_pack(frontmatter: dict[str, Any]) -> str:
    for key in ("pack", "pack_id", "packId"):
        value = frontmatter.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _infer_data_pack(
    *,
    name: str,
    frontmatter: dict[str, Any],
    overrides_base: bool,
) -> str:
    explicit = _frontmatter_pack(frontmatter)
    if explicit:
        return explicit
    if overrides_base:
        return "local-pack"
    return "local-pack"


def _pack_for(
    *,
    name: str,
    origin: CatalogOrigin,
    frontmatter: dict[str, Any],
    overrides_base: bool = False,
) -> str:
    if origin == "logic":
        return "base-pack"
    return _infer_data_pack(
        name=name,
        frontmatter=frontmatter,
        overrides_base=overrides_base,
    )


def _read_catalog_file(path: Path) -> tuple[dict[str, Any], str, str]:
    text = path.read_text(encoding="utf-8")
    frontmatter, content_body = _split_frontmatter(text)
    return frontmatter, content_body, text


def _iter_skill_paths(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        return {}
    out: dict[str, Path] = {}
    try:
        children = list(root.iterdir())
    except OSError as e:
        LOG.warning("skills catalog %s unreadable: %s", root, e)
        return {}
    for child in children:
        skill_file = child / "SKILL.md"
        if child.is_dir() and skill_file.is_file():
            out[child.name] = skill_file
    return out


def _iter_data_skill_paths(root: Path) -> dict[str, list[tuple[str | None, Path]]]:
    """Enumera le skill del catalogo DATA con supporto ai pack-subdir.

    Layout supportati sotto `root`:
      <skill>/SKILL.md              → skill flat (pack inferito dal frontmatter)
      <pack>/<skill>/SKILL.md       → skill in un pack esplicito (pack = <pack>)

    Ritorna name → lista di (pack_label|None, path): lo STESSO nome può esistere
    in più pack (es. `pdf` in anthropic-pack e openai-curated-pack)."""
    out: dict[str, list[tuple[str | None, Path]]] = {}
    if not root.is_dir():
        return out
    try:
        children = sorted(root.iterdir())  # ordine deterministico (coerente con skill_sync)
    except OSError as e:
        LOG.warning("skills catalog %s unreadable: %s", root, e)
        return out
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        flat = child / "SKILL.md"
        if flat.is_file():
            # skill flat: pack inferito a valle dal frontmatter/naming
            out.setdefault(child.name, []).append((None, flat))
            continue
        # altrimenti è (forse) una dir-pack: cerca <pack>/<skill>/SKILL.md
        try:
            grandchildren = sorted(child.iterdir())
        except OSError:
            continue
        for gc in grandchildren:
            sf = gc / "SKILL.md"
            if gc.is_dir() and sf.is_file():
                out.setdefault(gc.name, []).append((child.name, sf))
    return out


def _iter_rule_paths(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        return {}
    out: dict[str, Path] = {}
    try:
        children = list(root.glob("*.md"))
    except OSError as e:
        LOG.warning("rules catalog %s unreadable: %s", root, e)
        return {}
    for path in children:
        if path.name == "README.md":
            continue
        out[path.stem] = path
    return out


def _paths_for(
    kind: CatalogKind,
) -> tuple[dict[str, Path], dict[str, list[tuple[str | None, Path]]]]:
    """Ritorna (logic, data). `logic` = name→path (single, base-pack). `data` =
    name→lista di (pack_label|None, path): per le skill supporta i pack-subdir
    (più pack possono avere lo stesso nome); per le rule è una sola variante."""
    if kind == "skill":
        logic = _iter_skill_paths(LOGIC_SKILLS_DIR)
        if LOGIC_SKILLS_DIR.resolve() == DATA_SKILLS_DIR.resolve():
            return logic, {}
        return logic, _iter_data_skill_paths(DATA_SKILLS_DIR)
    logic = _iter_rule_paths(LOGIC_RULES_DIR)
    if LOGIC_RULES_DIR.resolve() == DATA_RULES_DIR.resolve():
        return logic, {}
    data = {n: [(None, p)] for n, p in _iter_rule_paths(DATA_RULES_DIR).items()}
    return logic, data


def _item_from_path(
    *,
    kind: CatalogKind,
    name: str,
    path: Path,
    source: CatalogSource,
    available_in: list[str],
    pack: str,
    variants: list[dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        frontmatter, content_body, _full_body = _read_catalog_file(path)
    except Exception as e:
        LOG.warning("%s catalog item %s unreadable at %s: %s", kind, name, path, e)
        return None
    description = (
        _skill_description(frontmatter)
        if kind == "skill"
        else _rule_description(content_body)
    )
    return {
        "name": name,
        "description": description,
        "source": source,
        "pack": pack,
        "available_packs": [v["pack"] for v in variants],
        "variants": variants,
        "path": str(path),
        "available_in": available_in,
    }


def _variant(
    *,
    origin: CatalogOrigin,
    path: Path,
    pack: str,
    active: bool,
) -> dict[str, Any]:
    return {
        "origin": origin,
        "source": origin,
        "pack": pack,
        "path": str(path),
        "active": active,
    }


def _list_catalog(kind: CatalogKind) -> list[dict[str, Any]]:
    cache = _LIST_CACHE[kind]
    if cache["data"] is not None and _cache_fresh(cache["ts"]):
        return cache["data"]

    logic, data = _paths_for(kind)
    names = sorted(set(logic) | set(data))
    out: list[dict[str, Any]] = []
    for name in names:
        in_logic = name in logic
        data_variants = data.get(name, [])
        in_data = bool(data_variants)
        source: CatalogSource = "both" if in_logic and in_data else "data" if in_data else "logic"
        available_in = [s for s, present in (("logic", in_logic), ("data", in_data)) if present]
        # effettivo: la prima variante data vince sul base-pack logic.
        effective_path = data_variants[0][1] if in_data else logic[name]
        variants: list[dict[str, Any]] = []
        if in_logic:
            variants.append(
                _variant(origin="logic", path=logic[name], pack="base-pack",
                         active=not in_data)
            )
        effective_pack = "base-pack"
        for idx, (pack_label, path) in enumerate(data_variants):
            try:
                fm, _b, _f = _read_catalog_file(path)
            except Exception:
                fm = {}
            pack = pack_label or _pack_for(
                name=name, origin="data", frontmatter=fm, overrides_base=in_logic)
            if idx == 0:
                effective_pack = pack
            variants.append(
                _variant(origin="data", path=path, pack=pack, active=(idx == 0))
            )
        item = _item_from_path(
            kind=kind,
            name=name,
            path=effective_path,
            source=source,
            available_in=available_in,
            pack=effective_pack,
            variants=variants,
        )
        if item is not None:
            out.append(item)

    cache["data"] = out
    cache["ts"] = time.time()
    return out


def _resolve_detail(kind: CatalogKind, name: str) -> dict[str, Any]:
    if not _NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail={"error": "invalid name"})

    cached = _DETAIL_CACHE[kind].get(name)
    if cached and _cache_fresh(cached["ts"]):
        return cached["data"]

    logic, data = _paths_for(kind)
    in_logic = name in logic
    data_variants = data.get(name, [])
    in_data = bool(data_variants)
    if not in_logic and not in_data:
        noun = "skill" if kind == "skill" else "rule"
        raise HTTPException(status_code=404, detail={"error": f"{noun} not found"})

    source: CatalogSource = "both" if in_logic and in_data else "data" if in_data else "logic"
    available_in = [s for s, present in (("logic", in_logic), ("data", in_data)) if present]
    path = data_variants[0][1] if in_data else logic[name]
    try:
        frontmatter, content_body, _full_body = _read_catalog_file(path)
    except Exception as e:
        LOG.warning("%s detail %s unreadable at %s: %s", kind, name, path, e)
        raise HTTPException(status_code=500, detail={"error": f"{kind} unreadable"})
    description = (
        _skill_description(frontmatter)
        if kind == "skill"
        else _rule_description(content_body)
    )
    variants: list[dict[str, Any]] = []
    if in_logic:
        variants.append(
            _variant(origin="logic", path=logic[name], pack="base-pack",
                     active=not in_data)
        )
    pack = "base-pack"
    for idx, (pack_label, vpath) in enumerate(data_variants):
        try:
            vfm, _vb, _vf = _read_catalog_file(vpath)
        except Exception:
            vfm = {}
        vpack = pack_label or _pack_for(
            name=name, origin="data", frontmatter=vfm, overrides_base=in_logic)
        if idx == 0:
            pack = vpack
        variants.append(
            _variant(origin="data", path=vpath, pack=vpack, active=(idx == 0))
        )
    data_out = {
        "name": name,
        "description": description,
        "source": source,
        "pack": pack,
        "available_packs": [v["pack"] for v in variants],
        "variants": variants,
        "path": str(path),
        "available_in": available_in,
        "frontmatter": frontmatter,
        "body": content_body,
    }
    _DETAIL_CACHE[kind][name] = {"ts": time.time(), "data": data_out}
    return data_out


@router.get("/clodia/skills")
async def list_skills() -> list[dict[str, Any]]:
    return _list_catalog("skill")


@router.get("/clodia/rules")
async def list_rules() -> list[dict[str, Any]]:
    return _list_catalog("rule")


@router.get("/clodia/skills/{name}")
async def get_skill(name: str):
    try:
        return _resolve_detail("skill", name)
    except HTTPException as e:
        if isinstance(e.detail, dict) and "error" in e.detail:
            return JSONResponse(status_code=e.status_code, content=e.detail)
        raise


@router.get("/clodia/rules/{name}")
async def get_rule(name: str):
    try:
        return _resolve_detail("rule", name)
    except HTTPException as e:
        if isinstance(e.detail, dict) and "error" in e.detail:
            return JSONResponse(status_code=e.status_code, content=e.detail)
        raise


# ---------------------------------------------------------------------------
# Skill CRUD (solo pack utente)
#
# Le skill native (base-pack, dal catalogo logic/git) sono SOLA LETTURA. Il CRUD
# vale esclusivamente per le skill create dall'utente, che vivono nel data
# catalog (CLODIA_DATA/skills-catalog) e dichiarano `pack: user-pack` nel
# frontmatter. Così l'utente può aggiungere/modificare/eliminare le proprie
# skill senza toccare il patrimonio nativo.
# ---------------------------------------------------------------------------

USER_PACK = "user-pack"


class SkillImportUrl(BaseModel):
    url: str


def _invalidate(kind: CatalogKind) -> None:
    _LIST_CACHE[kind] = {"ts": 0.0, "data": None}
    _DETAIL_CACHE[kind].clear()


def _user_skill_dir(name: str) -> Path:
    """Path di una skill utente: pack-subdir `user-pack/<name>/`."""
    return DATA_SKILLS_DIR / USER_PACK / name


def _require_user_skill_dir(name: str) -> Path:
    """Verifica che `name` sia una skill del `user-pack` (rimovibile); ritorna la
    cartella. Rifiuta native (base-pack) e altri pack con 403/404."""
    if not _NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail={"error": "nome non valido"})
    if name in _iter_skill_paths(LOGIC_SKILLS_DIR):
        raise HTTPException(status_code=403,
                            detail={"error": f"'{name}' è una skill nativa (base-pack)"})
    d = _user_skill_dir(name)
    if not (d / "SKILL.md").is_file():
        raise HTTPException(status_code=404, detail={"error": "skill user-pack non trovata"})
    return d


@router.post("/clodia/skills/import")
async def import_skill_zip(file: UploadFile = File(...)):
    """Importa una skill da archivio .zip → pack `user-pack`."""
    from .skill_import import SkillImportError, import_zip
    data = await file.read()
    try:
        names = import_zip(data)
    except SkillImportError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": f"import fallito: {str(e)[:160]}"})
    return {"imported": names, "pack": USER_PACK}


@router.post("/clodia/skills/import-url")
async def import_skill_url(payload: SkillImportUrl):
    """Importa una skill da URL (git repo o .zip remoto) → pack `user-pack`."""
    from .skill_import import SkillImportError, import_url
    try:
        names = import_url(payload.url)
    except SkillImportError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": f"import fallito: {str(e)[:160]}"})
    return {"imported": names, "pack": USER_PACK}


@router.delete("/clodia/skills/{name}")
async def delete_skill(name: str):
    try:
        d = _require_user_skill_dir(name)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content=e.detail)
    shutil.rmtree(d, ignore_errors=True)
    _invalidate("skill")
    return {"deleted": name}
