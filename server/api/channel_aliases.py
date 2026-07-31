"""Macro del composer, isolate per principal e mai applicate sull'ingest."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..config import data_path
from .agents import _principal_from_request

router = APIRouter()
_ROOT = data_path("user-settings")
_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ALIAS_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")


def _principal(request: Request) -> str:
    principal = _principal_from_request(request)
    if not principal or not _PRINCIPAL_RE.fullmatch(principal):
        raise HTTPException(401, "principal non valido")
    return principal


def _path(principal: str) -> Path:
    return _ROOT / principal / "channel-aliases.json"


def _normalize(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise HTTPException(400, "aliases deve essere un mapping")
    aliases: dict[str, str] = {}
    for raw_key, raw_value in raw.items():
        key = str(raw_key or "").strip().removeprefix("$")
        value = str(raw_value or "").strip()
        if not _ALIAS_RE.fullmatch(key):
            raise HTTPException(400, f"alias non valido: {raw_key!r}")
        if not value:
            raise HTTPException(400, f"testo mancante per ${key}")
        aliases[key] = value
    return dict(sorted(aliases.items()))


def _read(principal: str) -> dict[str, str]:
    try:
        raw = json.loads(_path(principal).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, "impostazioni alias non leggibili") from exc
    return _normalize(raw)


@router.get("/api/channel-aliases")
async def get_channel_aliases(request: Request) -> dict:
    return {"aliases": _read(_principal(request))}


@router.put("/api/channel-aliases")
async def put_channel_aliases(request: Request) -> dict:
    principal = _principal(request)
    body = await request.json()
    aliases = _normalize(body.get("aliases", body))
    path = _path(principal)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(aliases, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return {"aliases": aliases}
