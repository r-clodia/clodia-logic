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
from fastapi import APIRouter, HTTPException, Request
from starlette.datastructures import UploadFile as StarletteUploadFile

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


# ── Jobs as data scopes (#670) ───────────────────────────────────────────────
#
# A job's imports live HERE, in the scheduler's records, not in the gateway:
# the catalogue learns from this side which jobs import a resource, and a
# delete removes the resource from them here, after the gateway has exported
# and dropped it (the gateway's tombstone already refuses every job call).

def _job_scopes() -> dict[str, list[str]]:
    """`{"<kind>:<name>": ["job:<id>", …]}` over every job's `data`."""
    from ..scheduler import db
    out: dict[str, list[str]] = {}
    for j in db.list_jobs():
        for e in j.get("data") or []:
            if isinstance(e, dict) and e.get("kind") and e.get("name"):
                out.setdefault(f"{e['kind']}:{e['name']}", []).append(f"job:{j['id']}")
    return out


def _drop_from_jobs(kind: str, name: str, scopes: list[str]) -> tuple[list[str], list[dict]]:
    from ..scheduler import db
    removed, failed = [], []
    for s in scopes:
        try:
            jid = int(s.split(":", 1)[1])
            job = db.get_job(jid)
            if job is None:
                continue
            left = [e for e in job.get("data") or []
                    if not (e.get("kind") == kind and e.get("name") == name)]
            db.set_job_data(jid, left)
            removed.append(s)
        except Exception as e:  # noqa: BLE001 — reported; the tombstone already cuts access
            failed.append({"scope": s, "error": str(e)[:200]})
    return removed, failed


@router.get("/clodia/databases")
async def get_catalogue(request: Request) -> dict:
    who = _person(request)
    out = await _acall(who, "GET", "")
    if out.get("is_admin"):
        # Admins see every scope importing a resource, jobs included.
        try:
            jobs = await asyncio.to_thread(_job_scopes)
        except Exception as e:  # noqa: BLE001 — the catalogue stays readable
            LOG.warning("databases: job imports not read (%s)", type(e).__name__)
            jobs = {}
        for e in out.get("entries") or []:
            e["imported_by_scopes"] = list(e.get("imported_by_scopes") or []) + \
                jobs.get(e.get("key") or "", [])
    return out


@router.post("/clodia/databases")
async def post_catalogue(request: Request) -> dict:
    who = _person(request)
    if not admin.is_admin(who):
        # Recorded as a refused control-plane action by `audit_mw` (status 403).
        raise HTTPException(403, "creare ed eliminare datastore e collection è riservato agli admin")
    body = await _body(request)
    if body.get("action") not in _ADMIN_ACTIONS:
        raise HTTPException(400, "azione: " + " | ".join(_ADMIN_ACTIONS))
    if body.get("action") != "delete":
        return await _acall(who, "POST", "", json=body)
    kind, name = str(body.get("kind") or ""), str(body.get("name") or "")
    jobs = (await asyncio.to_thread(_job_scopes)).get(f"{kind}:{name}", [])
    out = await _acall(who, "POST", "", json={**body, "job_scopes": jobs},
                       timeout=_DELETE_TIMEOUT)
    removed, failed = await asyncio.to_thread(_drop_from_jobs, kind, name, jobs)
    out["imports_removed"] = list(out.get("imports_removed") or []) + removed
    out["imports_failed"] = list(out.get("imports_failed") or []) + failed
    return out


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


#: Room for the multipart envelope and the small form fields around the file.
_MULTIPART_OVERHEAD = 64 * 1024


@router.post("/clodia/databases/collection/{name}/upload")
async def collection_upload(name: str, request: Request) -> dict:
    """Multipart (`file`, `doc_name?`, `version?`, `scope?`). The size ceiling
    is enforced BEFORE the body is parsed (#671 review): a declared
    Content-Length above it is refused at once, and the body is read with a
    cap — a form parameter would have let Starlette spool the whole upload to
    disk first."""
    who = _person(request)
    limit = _upload_max()
    cap = limit + _MULTIPART_OVERHEAD
    declared = request.headers.get("content-length") or ""
    if declared.isdigit() and int(declared) > cap:
        raise HTTPException(413, f"file troppo grande (max {limit // (1024 * 1024)} MB)")
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > cap:
            raise HTTPException(413, f"file troppo grande (max {limit // (1024 * 1024)} MB)")
    from starlette.requests import Request as _Req
    body = bytes(raw)
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}
    form = await _Req(request.scope, receive).form()
    try:
        file = form.get("file")
        if not isinstance(file, StarletteUploadFile):
            raise HTTPException(400, "campo 'file' richiesto")
        data = await file.read()
        filename = file.filename or ""
        fields = {k: str(form.get(k) or "") for k in ("doc_name", "version", "scope")}
    finally:
        await form.close()
    if len(data) > limit:
        raise HTTPException(413, f"file troppo grande (max {limit // (1024 * 1024)} MB)")
    params = {"filename": filename, "doc_name": fields["doc_name"],
              "version": fields["version"] or "1"}
    if fields["scope"]:
        params["scope"] = fields["scope"]
    return await _acall(who, "POST", f"/collection/{_seg(name)}/upload", params=params,
                        data=data, headers={"Content-Type": "application/octet-stream"},
                        timeout=_UPLOAD_TIMEOUT)


@router.post("/clodia/databases/collection/{name}/remove")
async def collection_remove(name: str, request: Request) -> dict:
    who = _person(request)
    body = await _body(request)
    return await _acall(who, "POST", f"/collection/{_seg(name)}/remove", json=body)
