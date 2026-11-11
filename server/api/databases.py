"""The Databases page's door (clodia-platform#671, #672).

The gateway holds the catalogue, the data and the decision
(clodia-tools `databases.py`, `databases_api.py`). Here: the PERSON who asks,
forwarded with an on-behalf token that carries their signed role and clearance
— the same token `gateway_pdp` mints for every human action. Nothing is opened
or decided on this side: a second copy of the rules would be the copy without
audit.

    GET  /clodia/databases                                      the catalogue
    POST /clodia/databases {action: create_datastore | create_collection
                            | delete | restore, ...}            admins
    GET  /clodia/databases/datastore/{pack}/{name}/tables       ?scope=
    GET  /clodia/databases/datastore/{pack}/{name}/rows         ?table&limit&offset&q&scope
    POST /clodia/databases/datastore/{pack}/{name}/rows         {op, table, rowid?, values?, scope?}
    POST /clodia/databases/datastore/{pack}/{name}/schema       {op, ...}  admins
    GET  /clodia/databases/collection/{name}/documents          ?scope=
    GET  /clodia/databases/collection/{name}/chunks             ?doc&version&limit&offset&scope
    POST /clodia/databases/collection/{name}/upload             multipart: file, doc_name?, version?, scope?
    POST /clodia/databases/collection/{name}/remove             {doc_name, version?, scope?}
"""
from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import quote

import requests
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from . import admin, gateway_pdp

LOG = logging.getLogger("agent-server.api.databases")
router = APIRouter()

#: Same ceiling as the gateway's (`CLODIA_DB_UPLOAD_MAX_BYTES`, default 20 MB):
#: checked here too so that an oversized file is not streamed across twice.
_UPLOAD_MAX = 20 * 1024 * 1024
_TIMEOUT = 60
#: Extraction + embedding of a large document runs inside the request.
_UPLOAD_TIMEOUT = 330
#: A delete exports the whole collection first.
_DELETE_TIMEOUT = 330

_ADMIN_ACTIONS = ("create_datastore", "create_collection", "delete", "restore")


def _upload_max() -> int:
    try:
        return int(os.environ.get("CLODIA_DB_UPLOAD_MAX_BYTES") or _UPLOAD_MAX)
    except ValueError:
        return _UPLOAD_MAX


def _person(request: Request) -> str:
    from .agents import _principal_from_request
    who = _principal_from_request(request)
    if not who:
        raise HTTPException(401, "autenticazione richiesta")
    if not admin.is_human(who):
        raise HTTPException(403, "la pagina Databases è di una persona, non di un agente")
    return who


def _call(who: str, method: str, path: str, *, timeout: float = _TIMEOUT, **kw) -> dict:
    url = f"{gateway_pdp._gw_base()}/internal/databases{path}"
    headers = {"Authorization": f"Bearer {gateway_pdp._token(who)}", **(kw.pop("headers", None) or {})}
    try:
        r = gateway_pdp._gw_http.request(method, url, headers=headers, timeout=timeout, **kw)
    except requests.RequestException as e:
        raise HTTPException(502, f"gateway irraggiungibile: {e}") from e
    try:
        data = r.json()
    except ValueError:
        data = {"error": (r.text or "")[:200]}
    if r.status_code != 200:
        detail = (data or {}).get("error") or (data or {}).get("detail") or f"HTTP {r.status_code}"
        raise HTTPException(r.status_code if 400 <= r.status_code < 600 else 502, detail)
    return data


async def _acall(who: str, method: str, path: str, **kw) -> dict:
    return await asyncio.to_thread(_call, who, method, path, **kw)


def _seg(s: str) -> str:
    return quote(s, safe="")


def _params(request: Request, *keys: str) -> dict:
    return {k: request.query_params[k] for k in keys if request.query_params.get(k) not in (None, "")}


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "corpo JSON non valido") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "corpo JSON: atteso un oggetto")
    return body


@router.get("/clodia/databases")
async def get_catalogue(request: Request) -> dict:
    return await _acall(_person(request), "GET", "")


@router.post("/clodia/databases")
async def post_catalogue(request: Request) -> dict:
    who = _person(request)
    if not admin.is_admin(who):
        raise HTTPException(403, "creare ed eliminare datastore e collection è riservato agli admin")
    body = await _body(request)
    if body.get("action") not in _ADMIN_ACTIONS:
        raise HTTPException(400, "azione: " + " | ".join(_ADMIN_ACTIONS))
    return await _acall(who, "POST", "", json=body,
                        timeout=_DELETE_TIMEOUT if body.get("action") == "delete" else _TIMEOUT)


@router.get("/clodia/databases/datastore/{pack}/{name}/tables")
async def datastore_tables(pack: str, name: str, request: Request) -> dict:
    return await _acall(_person(request), "GET",
                        f"/datastore/{_seg(pack)}/{_seg(name)}/tables",
                        params=_params(request, "scope"))


@router.get("/clodia/databases/datastore/{pack}/{name}/rows")
async def datastore_rows(pack: str, name: str, request: Request) -> dict:
    return await _acall(_person(request), "GET",
                        f"/datastore/{_seg(pack)}/{_seg(name)}/rows",
                        params=_params(request, "table", "limit", "offset", "q", "scope"))


@router.post("/clodia/databases/datastore/{pack}/{name}/rows")
async def datastore_row_op(pack: str, name: str, request: Request) -> dict:
    who = _person(request)
    body = await _body(request)
    if body.get("op") not in ("insert", "update", "delete"):
        raise HTTPException(400, "op: insert | update | delete")
    return await _acall(who, "POST", f"/datastore/{_seg(pack)}/{_seg(name)}/rows", json=body)


@router.post("/clodia/databases/datastore/{pack}/{name}/schema")
async def datastore_schema(pack: str, name: str, request: Request) -> dict:
    who = _person(request)
    if not admin.is_admin(who):
        raise HTTPException(403, "le modifiche dello schema sono riservate agli admin")
    body = await _body(request)
    return await _acall(who, "POST", f"/datastore/{_seg(pack)}/{_seg(name)}/schema", json=body)


@router.get("/clodia/databases/collection/{name}/documents")
async def collection_documents(name: str, request: Request) -> dict:
    return await _acall(_person(request), "GET", f"/collection/{_seg(name)}/documents",
                        params=_params(request, "scope"))


@router.get("/clodia/databases/collection/{name}/chunks")
async def collection_chunks(name: str, request: Request) -> dict:
    return await _acall(_person(request), "GET", f"/collection/{_seg(name)}/chunks",
                        params=_params(request, "doc", "version", "limit", "offset", "scope"))


@router.post("/clodia/databases/collection/{name}/upload")
async def collection_upload(name: str, request: Request, file: UploadFile = File(...),
                            doc_name: str = Form(""), version: str = Form("1"),
                            scope: str = Form("")) -> dict:
    who = _person(request)
    limit = _upload_max()
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"file troppo grande (max {limit // (1024 * 1024)} MB)")
    params = {"filename": file.filename or "", "doc_name": doc_name, "version": version or "1"}
    if scope:
        params["scope"] = scope
    return await _acall(who, "POST", f"/collection/{_seg(name)}/upload", params=params,
                        data=data, headers={"Content-Type": "application/octet-stream"},
                        timeout=_UPLOAD_TIMEOUT)


@router.post("/clodia/databases/collection/{name}/remove")
async def collection_remove(name: str, request: Request) -> dict:
    who = _person(request)
    body = await _body(request)
    return await _acall(who, "POST", f"/collection/{_seg(name)}/remove", json=body)
