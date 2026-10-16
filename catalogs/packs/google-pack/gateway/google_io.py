"""google_io — the one way google-pack's gateway component talks to a Google API.

Moved from clodia-tools `server/tools/google_io.py` (clodia-platform#524 step 2,
#558): every Drive, Docs, Sheets and Calendar call of the component goes
through `call()` — the 24 `gdrive/gsheets/gdocs/gcalendar` verbs, the
`gdrive_root` parent walk and adoption, the folder labels, and the services
the gateway calls (the pre-PDP parent lookup of `gdrive.update`, the folder
description of the confinement API). What decides — the perimeter, the scratch
I/O — stays in the callers; this module only moves bytes.

The transport is plain REST over `urllib`, no `googleapiclient`:

- the URL is `<root of api/version>/<path>`, path parameters percent-encoded
  as the discovery documents' simple expansion does;
- JSON methods get `alt=json` (not a DELETE, which has no body), booleans
  travel as `true`/`false`, `None` parameters are dropped — the three things
  the discovery client did to the query string;
- an upload WITH metadata is `uploadType=multipart` (multipart/related: JSON
  part, then the media), an upload WITHOUT metadata is `uploadType=media` —
  the two non-resumable forms the verbs use; there is no resumable upload;
- a download is one GET with `alt=media` returning the bytes;
- a non-2xx answer raises `HttpError` (same name as the discovery client's);
  a 401 drops the cached token and retries once with a fresh one.

The access token. `access_token(account)` refreshes it from the `google_<acct>`
(or legacy `gworkspace_<acct>`) bundle, read through the host's scoped
credentials (`_host.read_internal`, audited by the gateway's vault), with the
stdlib (token endpoint, `grant_type=refresh_token`), and caches it per
credential until `expires_in` minus `TOKEN_MARGIN_S`. The token is never
logged and never part of an exception message; the refresh token never leaves
the gateway process. Nothing here imports the gateway: this file runs inside
it, reached through the host only.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional, Union

from . import _host

LOG = logging.getLogger("clodia-tools.component.google-pack.google-io")

TOKEN_URL = "https://oauth2.googleapis.com/token"
#: Per-request timeout: a stalled call fails instead of holding a worker thread.
TIMEOUT_S = 30
#: Seconds before `expires_in` at which a cached token is no longer used.
TOKEN_MARGIN_S = 120

#: Root of each API, i.e. discovery `rootUrl + servicePath` (+ version for the
#: APIs whose method paths carry it).
API_ROOTS = {
    ("drive", "v3"): "https://www.googleapis.com/drive/v3/",
    ("docs", "v1"): "https://docs.googleapis.com/v1/",
    ("sheets", "v4"): "https://sheets.googleapis.com/v4/",
    ("calendar", "v3"): "https://www.googleapis.com/calendar/v3/",
}
#: Media upload roots (discovery `mediaUpload.protocols.simple.path`).
UPLOAD_ROOTS = {
    ("drive", "v3"): "https://www.googleapis.com/upload/drive/v3/",
}


class HttpError(RuntimeError):
    """A non-2xx answer from a Google API. Carries the status, never a token."""

    def __init__(self, status: int, method: str, url: str, reason: str,
                 content: bytes = b""):
        self.status = int(status)
        self.method = method
        self.url = url
        self.reason = reason
        self.content = content
        super().__init__(
            f'<HttpError {self.status} when requesting {method} {url} returned "{reason}">')


class RefreshError(RuntimeError):
    """The token endpoint refused the refresh token (revoked, expired, …)."""


@dataclass(frozen=True)
class Media:
    """A local file to upload. `mimetype=None` guesses it from the name, as
    `MediaFileUpload` did (`application/octet-stream` when the guess fails)."""

    path: str
    mimetype: Optional[str] = None

    def content_type(self) -> str:
        return self.mimetype or mimetypes.guess_type(self.path)[0] \
            or "application/octet-stream"


# ---------------------------------------------------------------------------
# the request
# ---------------------------------------------------------------------------

def request(api: str, version: str, method_path: str, *,
            params: Optional[dict] = None, body: Any = None,
            media: Optional[Media] = None, download: bool = False) -> dict:
    """The transport request for `method_path` (`"GET files/{fileId}"`).

    Path parameters are taken out of `params` by name; what remains is the
    query string. `None` values are dropped, as the discovery client did.
    """
    if (api, version) not in API_ROOTS:
        raise ValueError(f"google_io: unknown API {api}/{version}")
    method, _, template = (method_path or "").strip().partition(" ")
    method, template = method.upper(), template.strip()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE") or not template:
        raise ValueError(f"google_io: bad method_path {method_path!r}")
    if media is not None and (api, version) not in UPLOAD_ROOTS:
        raise ValueError(f"google_io: {api}/{version} has no media upload")
    query = {k: v for k, v in (params or {}).items() if v is not None}
    path_params: dict[str, str] = {}
    path = template
    while "{" in path:
        start = path.index("{")
        end = path.index("}", start)
        name = path[start + 1:end]
        if name not in query:
            raise ValueError(f"google_io: missing path parameter {name!r} for {method_path}")
        value = str(query.pop(name))
        path_params[name] = value
        path = path[:start] + urllib.parse.quote(value, safe="") + path[end + 1:]
    return {"api": api, "version": version, "method": method, "path": path,
            "template": template, "path_params": path_params, "params": query,
            "body": body, "media": media, "download": bool(download)}


def call(account: str, api: str, version: str, method_path: str, *,
         params: Optional[dict] = None, body: Any = None,
         media: Optional[Media] = None,
         download: bool = False) -> Union[dict, bytes]:
    """Perform one Google API call for the (already resolved) `account`.

    Returns the decoded JSON (`{}` for an empty answer), or the raw bytes when
    `download=True`.
    """
    req = request(api, version, method_path, params=params, body=body,
                  media=media, download=download)
    return _transport(account, req)


def _http_transport(account: str, req: dict) -> Union[dict, bytes]:
    """The REST transport, in the gateway's process."""
    try:
        return _send(access_token(account), req)
    except HttpError as e:
        if e.status != 401:
            raise
    # The cached token was revoked or rotated under us: one fresh attempt.
    forget(account)
    return _send(access_token(account), req)


#: The transport. Tests replace it with a fake service.
_transport = _http_transport


def _query(req: dict) -> str:
    q: list[tuple[str, str]] = []
    params = dict(req["params"])
    if req["download"]:
        params["alt"] = "media"
    elif req["method"] != "DELETE":
        # A DELETE has no response body (discovery: no `response`), and the
        # discovery client sent it without `alt=json`.
        params.setdefault("alt", "json")
    if req["media"] is not None:
        params["uploadType"] = "multipart" if req["body"] is not None else "media"
    for k, v in params.items():
        for item in (v if isinstance(v, (list, tuple)) else [v]):
            if isinstance(item, bool):
                item = "true" if item else "false"
            q.append((k, str(item)))
    return urllib.parse.urlencode(q)


def _multipart(meta: Any, media: Media, payload: bytes) -> tuple[bytes, str]:
    boundary = "===============" + secrets.token_hex(16) + "=="
    head = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            + json.dumps(meta) + f"\r\n--{boundary}\r\n"
            + f"Content-Type: {media.content_type()}\r\n"
            + "Content-Transfer-Encoding: binary\r\n\r\n").encode("utf-8")
    tail = f"\r\n--{boundary}--".encode("ascii")
    return head + payload + tail, f'multipart/related; boundary="{boundary}"'


def _send(token: str, req: dict) -> Union[dict, bytes]:
    key = (req["api"], req["version"])
    root = UPLOAD_ROOTS[key] if req["media"] is not None else API_ROOTS[key]
    url = f"{root}{req['path']}?{_query(req)}"
    headers = {"Authorization": f"Bearer {token}"}
    if not req["download"]:
        headers["Accept"] = "application/json"
    data: Optional[bytes] = None
    media = req["media"]
    if media is not None:
        with open(media.path, "rb") as fh:
            payload = fh.read()
        if req["body"] is not None:
            data, headers["Content-Type"] = _multipart(req["body"], media, payload)
        else:
            data, headers["Content-Type"] = payload, media.content_type()
    elif req["body"] is not None:
        data = json.dumps(req["body"]).encode("utf-8")
        headers["Content-Type"] = "application/json"
    shown = f"{root}{req['path']}"          # no query string in errors or logs
    r = urllib.request.Request(url, data=data, method=req["method"], headers=headers)
    try:
        with _open(r, timeout=TIMEOUT_S) as resp:
            content = resp.read()
    except urllib.error.HTTPError as e:
        try:
            content = e.read() or b""
        except Exception:  # noqa: BLE001
            content = b""
        raise HttpError(e.code, req["method"], shown, _reason(content, e.reason),
                        content) from None
    if req["download"]:
        return content
    if not content.strip():
        return {}
    return json.loads(content.decode("utf-8"))


def _reason(content: bytes, fallback: Any) -> str:
    try:
        err = json.loads(content.decode("utf-8")).get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
    except Exception:  # noqa: BLE001 — a 5xx page is not JSON
        pass
    return str(fallback or "")


# ---------------------------------------------------------------------------
# the access token
# ---------------------------------------------------------------------------

class _NoTokenAcrossHosts(urllib.request.HTTPRedirectHandler):
    """Follow a redirect, but never carry the bearer token off its host.

    urllib copies every header to the redirected request, so `Authorization`
    would follow a 3xx to any host, even over plain http; httplib2 (what
    googleapiclient used) did not forward it (review of clodia-tools#380).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        prima = urllib.parse.urlsplit(req.full_url)
        dopo = urllib.parse.urlsplit(new.full_url)
        if (dopo.scheme != "https" or dopo.hostname != prima.hostname
                or dopo.port != prima.port):
            for h in list(new.headers):
                if h.lower() == "authorization":
                    del new.headers[h]
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_OPENER = urllib.request.build_opener(_NoTokenAcrossHosts())


def _open(req, timeout):
    return _OPENER.open(req, timeout=timeout)


#: credential name → (access token, monotonic deadline)
_tokens: dict[str, tuple[str, float]] = {}
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(cred: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(cred, threading.Lock())


def _cred(account: str) -> str:
    from .gdrive import _drive_cred
    return _drive_cred(account)


def access_token(account: str) -> str:
    """A valid access token for `account`, refreshed when missing or close to
    expiry. The vault is read (through the host) only when a refresh is needed."""
    cred = _cred(account)
    hit = _tokens.get(cred)
    if hit and time.monotonic() < hit[1]:
        return hit[0]
    with _lock_for(cred):
        hit = _tokens.get(cred)
        if hit and time.monotonic() < hit[1]:
            return hit[0]
        bundle = _host.read_internal(cred)
        token, expires_in = _refresh(bundle)
        _tokens[cred] = (token, time.monotonic() + max(0, expires_in - TOKEN_MARGIN_S))
        return token


def forget(account: Optional[str] = None) -> None:
    """Drop the cached token of `account` (all accounts when None)."""
    if account is None:
        _tokens.clear()
        return
    _tokens.pop(_cred(account), None)


def _refresh(bundle: dict) -> tuple[str, int]:
    """`grant_type=refresh_token` against the token endpoint, as google-auth
    did: client id/secret, refresh token, and the bundle's scope when present."""
    form = {"client_id": bundle["client_id"], "client_secret": bundle["client_secret"],
            "refresh_token": bundle["refresh_token"], "grant_type": "refresh_token"}
    if (bundle.get("scope") or "").strip():
        form["scope"] = bundle["scope"].strip()
    r = urllib.request.Request(
        TOKEN_URL, data=urllib.parse.urlencode(form).encode("ascii"), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        with _open(r, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads((e.read() or b"{}").decode("utf-8"))
        except Exception:  # noqa: BLE001
            err = {}
        code = str(err.get("error") or f"http {e.code}")
        desc = str(err.get("error_description") or "").strip()
        raise RefreshError(f"{code}: {desc}" if desc else code) from None
    token = data.get("access_token")
    if not token:
        raise RefreshError("the token endpoint returned no access_token")
    try:
        expires_in = int(data.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    return str(token), expires_in


__all__ = ["call", "request", "access_token", "forget", "Media", "HttpError",
           "RefreshError", "API_ROOTS", "UPLOAD_ROOTS"]
