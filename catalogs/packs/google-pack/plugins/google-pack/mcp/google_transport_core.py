#!/usr/bin/env python3
"""google_transport — the Google API transport of google-pack's `google` connector.

clodia-platform#524. The gateway keeps the 24 `gdrive/gsheets/gdocs/gcalendar`
verbs, the whole folder/calendar perimeter (`gdrive_root`) and the Google
credentials: OAuth consent, the app client, the `google_<acct>` bundles and the
token refresh. With the instance switch `connectors: {google: pack}` it builds
each Google API request itself, refreshes the access token itself, and hands
the request to the one tool here, `request` — the `google.request` contract of
`server/connectors/SCHEMA.md` («The switch») in clodia-tools:

    request({access_token, api, version, method, path, params, body,
             media_b64, media_mime, download})
      -> {"status": int, "json"?: any, "bytes_b64"?: str}

The request is sent EXACTLY as the gateway's in-process client sends it
(`server/tools/google_io.py`, `_send`/`_query`/`_multipart`):

- URL = root of `(api, version)` in `API_ROOTS` (`UPLOAD_ROOTS` when there is
  media) + `path`, the path parameters already percent-encoded by the gateway;
- query = `params`, plus `alt=media` for a download, else `alt=json` unless the
  method is DELETE, plus `uploadType=multipart` (media with body) or
  `uploadType=media` (media alone); booleans as `true`/`false`, a list as a
  repeated key, in the order received;
- `Authorization: Bearer <access_token>`, `Accept: application/json` unless
  downloading, a JSON body as `application/json`, media with metadata as
  `multipart/related` (JSON part, then the media), media alone with its own
  type;
- timeout 30 s; a redirect is followed, but the token never crosses to another
  host or to plain http.

The answer: `json` for a JSON body (absent when the body is empty), `bytes_b64`
for a download and for a non-JSON error body. A non-2xx status is an ANSWER,
not a failure: the gateway turns it into the same `HttpError` its in-process
path raises (and retries a 401 once with a fresh token). Anything else — a bad
request, a network error, a 2xx that is not JSON outside a download — is a
failure (`ToolFailure`) whose message never carries the token, the arguments
or the URL: the gateway logs it by type and treats the call as failed, and the
perimeter then fails closed.

The process holds no state: no token cache, no file, nothing between calls.
The refresh token and the client secret never reach it.

**Stdlib only**: the MCP envelope is `google_transport_mcp.py`, so this module
can be tested without the gateway's `mcp`. The tool is gateway-internal: the
gateway hides it from agents, who keep calling `gdrive.*`/`gsheets.*`/….
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

__version__ = "1.0.0"

#: Per-request timeout, as `google_io.TIMEOUT_S`.
TIMEOUT_S = 30

#: Root of each API — `google_io.API_ROOTS`, verbatim.
API_ROOTS = {
    ("drive", "v3"): "https://www.googleapis.com/drive/v3/",
    ("docs", "v1"): "https://docs.googleapis.com/v1/",
    ("sheets", "v4"): "https://sheets.googleapis.com/v4/",
    ("calendar", "v3"): "https://www.googleapis.com/calendar/v3/",
}
#: Media upload roots — `google_io.UPLOAD_ROOTS`, verbatim.
UPLOAD_ROOTS = {
    ("drive", "v3"): "https://www.googleapis.com/upload/drive/v3/",
}

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
ARGS = ("access_token", "api", "version", "method", "path", "params", "body",
        "media_b64", "media_mime", "download")

#: What a method path may contain once the gateway expanded it: unreserved
#: characters, percent-escapes, `/` between segments and `:` before a custom
#: method (`values/<range>:append`). Nothing that could end the path (`?`,
#: `#`), change the authority, or smuggle a header.
_PATH = re.compile(r"[A-Za-z0-9._~%:/-]{1,2048}")
#: A token is a single header value: printable ASCII, no spaces.
_TOKEN = re.compile(r"[!-~]{1,4096}")
#: A media type: printable ASCII with spaces (parameters), no CR/LF.
_MIME = re.compile(r"[!-~][ -~]{0,254}")


class RequestError(ValueError):
    """A request outside the contract. The message names the field, never the
    value: the arguments carry the access token."""


class ToolFailure(RuntimeError):
    """The call failed. The message is generic by construction."""


# ── request validation ────────────────────────────────────────────────────

def _scalar(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool))


def _check(args: Any) -> dict:
    if not isinstance(args, dict):
        raise RequestError("arguments must be an object")
    unknown = set(args) - set(ARGS)
    if unknown:
        raise RequestError(f"unknown argument(s): {sorted(unknown)}")
    token = args.get("access_token")
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise RequestError("invalid 'access_token'")
    key = (args.get("api"), args.get("version"))
    if key not in API_ROOTS:
        raise RequestError("unknown 'api'/'version'")
    method = args.get("method")
    if method not in METHODS:
        raise RequestError("invalid 'method'")
    path = args.get("path")
    if not isinstance(path, str) or not _PATH.fullmatch(path):
        raise RequestError("invalid 'path'")
    # The path is appended to a fixed root: no empty or dot segment, so it can
    # neither climb out of the API root nor start a new authority (`//`).
    if any(seg in ("", ".", "..") for seg in path.split("/")):
        raise RequestError("invalid 'path'")
    params = args.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise RequestError("'params' must be an object")
    for k, v in params.items():
        if not isinstance(k, str) or not k:
            raise RequestError("invalid key in 'params'")
        if v is None or _scalar(v):
            continue
        if isinstance(v, list) and all(_scalar(i) for i in v):
            continue
        raise RequestError("invalid value in 'params'")
    media_b64 = args.get("media_b64")
    media: Optional[bytes] = None
    if media_b64 is not None:
        if not isinstance(media_b64, str):
            raise RequestError("'media_b64' must be base64 text")
        if key not in UPLOAD_ROOTS:
            raise RequestError("this api has no media upload")
        try:
            media = base64.b64decode(media_b64, validate=True)
        except (binascii.Error, ValueError):
            raise RequestError("'media_b64' is not valid base64") from None
    mime = args.get("media_mime")
    if mime is not None and (not isinstance(mime, str) or not _MIME.fullmatch(mime)):
        raise RequestError("invalid 'media_mime'")
    download = args.get("download", False)
    if download is None:
        download = False
    if not isinstance(download, bool):
        raise RequestError("'download' must be a boolean")
    return {"token": token, "api": key[0], "version": key[1], "method": method,
            "path": path,
            # `None` dropped, as the gateway already did (`google_io.request`).
            "params": {k: v for k, v in params.items() if v is not None},
            "body": args.get("body"), "media": media,
            "media_mime": mime or "application/octet-stream",
            "download": download}


# ── the request, framed as google_io frames it ────────────────────────────

def _query(req: dict) -> str:
    """`google_io._query`, line for line."""
    q: list[tuple[str, str]] = []
    params = dict(req["params"])
    if req["download"]:
        params["alt"] = "media"
    elif req["method"] != "DELETE":
        params.setdefault("alt", "json")
    if req["media"] is not None:
        params["uploadType"] = "multipart" if req["body"] is not None else "media"
    for k, v in params.items():
        for item in (v if isinstance(v, (list, tuple)) else [v]):
            if isinstance(item, bool):
                item = "true" if item else "false"
            q.append((k, str(item)))
    return urllib.parse.urlencode(q)


def _multipart(meta: Any, mime: str, payload: bytes) -> tuple[bytes, str]:
    """`google_io._multipart`, line for line."""
    boundary = "===============" + secrets.token_hex(16) + "=="
    head = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            + json.dumps(meta) + f"\r\n--{boundary}\r\n"
            + f"Content-Type: {mime}\r\n"
            + "Content-Transfer-Encoding: binary\r\n\r\n").encode("utf-8")
    tail = f"\r\n--{boundary}--".encode("ascii")
    return head + payload + tail, f'multipart/related; boundary="{boundary}"'


def build(req: dict) -> urllib.request.Request:
    """The `urllib` request `google_io._send` would open for the same call."""
    key = (req["api"], req["version"])
    root = UPLOAD_ROOTS[key] if req["media"] is not None else API_ROOTS[key]
    url = f"{root}{req['path']}?{_query(req)}"
    headers = {"Authorization": f"Bearer {req['token']}"}
    if not req["download"]:
        headers["Accept"] = "application/json"
    data: Optional[bytes] = None
    if req["media"] is not None:
        if req["body"] is not None:
            data, headers["Content-Type"] = _multipart(req["body"], req["media_mime"],
                                                       req["media"])
        else:
            data, headers["Content-Type"] = req["media"], req["media_mime"]
    elif req["body"] is not None:
        data = json.dumps(req["body"]).encode("utf-8")
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(url, data=data, method=req["method"], headers=headers)


# ── the connection ────────────────────────────────────────────────────────

class _NoTokenAcrossHosts(urllib.request.HTTPRedirectHandler):
    """Follow a redirect, but never carry the bearer token off its host.

    urllib copies every header to the redirected request, so `Authorization`
    would follow a 3xx to any host, even over plain http. Same rule as the
    gateway's `google_io._NoTokenAcrossHosts`: the header survives only an
    https redirect to the same host and port.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        before = urllib.parse.urlsplit(req.full_url)
        after = urllib.parse.urlsplit(new.full_url)
        if (after.scheme != "https" or after.hostname != before.hostname
                or after.port != before.port):
            for h in list(new.headers):
                if h.lower() == "authorization":
                    del new.headers[h]
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_OPENER = urllib.request.build_opener(_NoTokenAcrossHosts())


def _open(r: urllib.request.Request, timeout: float):
    return _OPENER.open(r, timeout=timeout)


def _answer(status: int, content: bytes, *, download: bool, error: bool) -> dict:
    if download and not error:
        return {"status": status, "bytes_b64": base64.b64encode(content).decode("ascii")}
    if not content.strip():
        return {"status": status}
    try:
        return {"status": status, "json": json.loads(content.decode("utf-8"))}
    except (UnicodeDecodeError, ValueError):
        if error:
            return {"status": status,
                    "bytes_b64": base64.b64encode(content).decode("ascii")}
        # A 2xx that is not JSON: the in-process client fails decoding it, so
        # this is a failed call too, never an empty success.
        raise ToolFailure("google-transport: the API answered with a non-JSON body") from None


def request(args: dict) -> dict:
    """One Google API call, sent as the gateway would send it."""
    req = _check(args or {})
    r = build(req)
    try:
        with _open(r, timeout=TIMEOUT_S) as resp:
            status = int(getattr(resp, "status", None) or resp.getcode() or 200)
            content = resp.read()
    except urllib.error.HTTPError as e:
        try:
            content = e.read() or b""
        except Exception:  # noqa: BLE001
            content = b""
        return _answer(int(e.code), content, download=req["download"], error=True)
    except Exception as e:  # noqa: BLE001 — network, TLS, timeout
        # By type only: a URLError's text may carry the URL and its query.
        raise ToolFailure(f"google-transport: request failed ({type(e).__name__})") from None
    return _answer(status, content, download=req["download"], error=False)


# ── the tool surface ──────────────────────────────────────────────────────

def call(name: str, arguments: Any) -> str:
    """The tool call as the MCP envelope makes it: JSON text, or a `ToolFailure`
    whose message never echoes the arguments."""
    fn = DISPATCH.get(name)
    if fn is None:
        raise ToolFailure("google-transport: unknown tool")
    try:
        return json.dumps(fn(arguments if isinstance(arguments, dict) else {}))
    except ToolFailure:
        raise
    except RequestError as e:
        # Field names only, by construction of `_check`.
        raise ToolFailure(f"google-transport: {e}") from None
    except Exception as e:  # noqa: BLE001
        raise ToolFailure(f"google-transport: internal error ({type(e).__name__})") from None


TOOL_SPECS: list[dict] = [
    dict(name="request",
         description=(
             "GATEWAY-INTERNAL — hidden from agents, who use gdrive.*, gsheets.*, "
             "gdocs.* and gcalendar.*. Sends one Google API request exactly as "
             "the gateway's in-process client would (google.request contract, "
             "clodia-tools server/connectors/SCHEMA.md) with the short-lived "
             "access token of this call. Returns JSON {status, json?, bytes_b64?}."),
         inputSchema={
             "type": "object",
             "properties": {
                 "access_token": {"type": "string",
                                  "description": "Short-lived OAuth access token."},
                 "api": {"type": "string", "enum": sorted({a for a, _ in API_ROOTS})},
                 "version": {"type": "string"},
                 "method": {"type": "string", "enum": list(METHODS)},
                 "path": {"type": "string",
                          "description": "Method path, parameters already percent-encoded."},
                 "params": {"type": "object", "description": "Query parameters."},
                 "body": {"description": "JSON body, or the metadata of a multipart upload."},
                 "media_b64": {"type": ["string", "null"],
                               "description": "Base64 bytes to upload."},
                 "media_mime": {"type": ["string", "null"]},
                 "download": {"type": "boolean", "default": False},
             },
             "required": ["access_token", "api", "version", "method", "path"],
             "additionalProperties": False,
         }),
]

DISPATCH = {"request": request}
