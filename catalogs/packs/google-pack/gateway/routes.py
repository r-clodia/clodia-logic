"""OAuth2 consent, the app client, and the connection test — in the gateway.

Moved from clodia-tools `server/tools_api.py` (`google_app_status`,
`google_app_config`, `google_auth`, `google_connect`, `_test_google`;
clodia-platform#558). The routes are mounted by the gateway at
`/tools/google-pack/<path>` and, in `pack` position, the gateway's own
`/tools/google/{app,auth,connect}` (what the Tools page calls) forward to them.
Either way the gateway authenticates the caller first (a HUMAN admin or the
trusted core's UI token) and audits the call; nothing here re-checks it.

Credential custody does not move: the bundles land in the gateway's vault
through the host (`_host.deposit`, scoped to `google_` / `app_google_oauth`),
with the same name, shape, type and grant as before — so switching to the
component needs no re-consent, and switching back finds them where they were.
"""
from __future__ import annotations

import json
import secrets as _secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from starlette.requests import Request
from starlette.responses import JSONResponse

from . import _host
from . import google_io
from . import google_oauth as go

#: OAuth `state` values handed out by `auth`, valid for `_STATE_TTL` seconds.
_states: dict[str, dict] = {}
_STATE_TTL = 600


def _gc_states() -> None:
    now = time.time()
    for k in [k for k, v in _states.items() if v["exp"] < now]:
        _states.pop(k, None)


def _account_from_email(email: str) -> str:
    return email.split("@")[0].replace(".", "_")


def _app() -> dict | None:
    try:
        return _host.read_internal(go.APP_CREDENTIAL)
    except Exception:  # noqa: BLE001 — absent (VaultDenied) or unreadable
        return None


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

async def app_status(request: Request):
    """GET app — is the Google OAuth app (client id/secret) in the vault?"""
    app = _app()
    if app is None:
        return JSONResponse({"configured": False, "redirect_uri": go.DEFAULT_REDIRECT})
    return JSONResponse({"configured": True,
                         "redirect_uri": app.get("redirect_uri", go.DEFAULT_REDIRECT)})


async def app_config(request: Request):
    """POST app — deposit the OAuth app client. Body: {client_json} (the JSON
    downloaded from Google Cloud) or {client_id, client_secret, redirect_uri}."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "bad_json"}, status_code=400)
    cj = body.get("client_json")
    if cj:
        try:
            data = json.loads(cj) if isinstance(cj, str) else cj
            node = data.get("installed") or data.get("web") or data
            uris = node.get("redirect_uris") or []
            app = {"client_id": node.get("client_id"),
                   "client_secret": node.get("client_secret"),
                   "redirect_uri": uris[0] if uris else go.DEFAULT_REDIRECT}
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "bad_client_json"}, status_code=400)
    else:
        app = {"client_id": (body.get("client_id") or "").strip(),
               "client_secret": (body.get("client_secret") or "").strip(),
               "redirect_uri": (body.get("redirect_uri") or "").strip() or go.DEFAULT_REDIRECT}
    if not app["client_id"] or not app["client_secret"]:
        return JSONResponse({"error": "client_id e client_secret richiesti"}, status_code=400)
    _host.deposit(go.APP_CREDENTIAL, app, cred_type="google_app", grant_agents=[])
    _host.get().log.info("google app: OAuth client deposited (client_id=%s…)",
                         app["client_id"][:12])
    return JSONResponse({"configured": True, "redirect_uri": app["redirect_uri"]})


async def auth(request: Request):
    """GET auth — start the UNIFIED Google consent (Gmail + Drive + Docs + Calendar)."""
    app = _app()
    if app is None:
        return JSONResponse(
            {"error": "app_not_configured",
             "detail": f"manca la credenziale d'app '{go.APP_CREDENTIAL}' nella vault"},
            status_code=409)
    _gc_states()
    state = _secrets.token_urlsafe(24)
    _states[state] = {"exp": time.time() + _STATE_TTL}
    url = go.consent_url(app["client_id"], app.get("redirect_uri", go.DEFAULT_REDIRECT),
                         scope=go.UNIFIED_SCOPE, state=state, prompt="select_account consent")
    return JSONResponse({"auth_url": url, "state": state,
                         "redirect_uri": app.get("redirect_uri", go.DEFAULT_REDIRECT)})


async def connect(request: Request):
    """POST connect — exchange the consent code for ONE refresh token with every
    scope, saved as `google_<account>` (what `email.*` and the 24 verbs read)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "bad_json"}, status_code=400)
    code = (body.get("code") or "").strip()
    state = body.get("state") or ""
    if not code:
        return JSONResponse({"error": "missing_code"}, status_code=400)
    st = _states.pop(state, None)
    if state and st is None:
        return JSONResponse({"error": "invalid_or_expired_state"}, status_code=400)
    app = _app()
    if app is None:
        return JSONResponse({"error": "app_not_configured"}, status_code=409)
    if code.startswith("http"):
        code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query).get("code", [""])[0]
    try:
        res = go.exchange_code(app["client_id"], app["client_secret"], code,
                               app.get("redirect_uri", go.DEFAULT_REDIRECT))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "exchange_failed", "detail": str(e)[:200]},
                            status_code=502)
    rt = res.get("refresh_token")
    if not rt:
        return JSONResponse(
            {"error": "no_refresh_token",
             "detail": "Google non ha restituito un refresh_token. App in Testing? "
                       "Mettila In production e riprova."}, status_code=400)
    try:
        email = go.get_profile_email(res["access_token"])
    except Exception as e_profile:  # noqa: BLE001
        try:
            email = go.get_userinfo_email(res["access_token"])
        except Exception as e_ui:  # noqa: BLE001
            return JSONResponse(
                {"error": "profile_failed",
                 "detail": f"profilo: {str(e_profile)[:140]} · userinfo: {str(e_ui)[:100]}"},
                status_code=502)
    account = _account_from_email(email)
    _host.deposit(
        f"google_{account}",
        {"client_id": app["client_id"], "client_secret": app["client_secret"],
         "refresh_token": rt, "email": email, "account": account, "scope": go.UNIFIED_SCOPE},
        cred_type="oauth2_google", grant_agents=["clodia"],
    )
    # The cached access token of this account is dropped by the gateway's vault
    # on deposit (service `credential_changed`).
    return JSONResponse({"connected": True, "account": account, "email": email})


async def app(request: Request):
    """GET app → status, POST app → deposit (the gateway's `/tools/google/app`)."""
    if request.method == "POST":
        return await app_config(request)
    return await app_status(request)


#: `(path, methods, handler, auth)` mounted by `register`. `owner` and `admin`
#: both require a human admin (or the trusted core); `owner` marks the routes
#: that write credentials.
ROUTES = [
    ("app", ["GET", "POST"], app, "owner"),
    ("auth", ["GET"], auth, "admin"),
    ("connect", ["POST"], connect, "owner"),
]


# --------------------------------------------------------------------------
# the connection test (service `test_connection`)
# --------------------------------------------------------------------------

#: Readable names of the consent scopes, to say WHICH service is missing.
_SERVICES = {
    "https://mail.google.com/": "Gmail",
    "https://www.googleapis.com/auth/drive": "Drive",
    "https://www.googleapis.com/auth/documents": "Docs",
    "https://www.googleapis.com/auth/calendar": "Calendar",
}
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


def _service_name(scope: str) -> str:
    return _SERVICES.get(scope) or scope.rstrip("/").rsplit("/", 1)[-1]


def _probe(bundle: dict, expected: str) -> tuple[bool, str]:
    """ONE account: token refresh + one `userinfo` call (the gateway's
    `_prova_google`, on the component's stdlib HTTP). `invalid_grant` means a
    revoked consent or a refresh token displaced by a second consent."""
    form = {"client_id": bundle["client_id"], "client_secret": bundle["client_secret"],
            "refresh_token": bundle["refresh_token"], "grant_type": "refresh_token"}
    r = urllib.request.Request(
        go.TOKEN_URL, data=urllib.parse.urlencode(form).encode("ascii"), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        with google_io._open(r, timeout=15) as resp:
            tok = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            err = (json.loads((e.read() or b"{}").decode("utf-8")) or {}).get("error") or ""
        except Exception:  # noqa: BLE001 — a Google 5xx is not JSON
            err = ""
        if err == "invalid_grant":
            return False, ("consenso revocato o refresh token non più valido — "
                           "riconnetti l'account")
        return False, f"Google OAuth {e.code}{': ' + err if err else ''}"
    access = tok.get("access_token") or ""
    if not access:
        return False, "Google non ha restituito un access token"
    ui = urllib.request.Request(USERINFO_URL, headers={"Authorization": f"Bearer {access}"})
    try:
        with google_io._open(ui, timeout=15) as resp:
            info = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return False, f"userinfo {e.code} (token rinfrescato ma non accettato)"
    detail = f"ok ({info.get('email') or 'account senza email'})"
    granted = (tok.get("scope") or "").split()
    missing = [s for s in expected.split() if s not in granted] if granted else []
    named = [_service_name(s) for s in missing if s in _SERVICES]
    if named:
        detail += f" · fuori dal consenso: {', '.join(named)} (riconnetti per aggiungerli)"
    return True, detail


def test_connection() -> dict:
    """`{ok: bool|None, detail}` over every unified `google_<acct>` credential —
    the card the Tools page shows for `google`. Never a secret in `detail`."""
    results, all_ok = [], True
    for name in sorted(_host.names("google_")):
        account = name[len("google_"):]
        try:
            b = _host.read_internal(name)
        except Exception:  # noqa: BLE001 — never the vault's internal reason
            results.append(f"{account}: credenziale illeggibile")
            all_ok = False
            continue
        missing = [f for f in ("client_id", "client_secret", "refresh_token")
                   if not b.get(f)]
        if missing:
            results.append(f"{account}: mancano {', '.join(missing)} — riconnetti")
            all_ok = False
            continue
        try:
            ok, detail = _probe(b, go.UNIFIED_SCOPE)
        except (urllib.error.URLError, OSError) as e:
            ok, detail = False, f"rete: {str(e)[:120]}"
        results.append(f"{account}: {detail}")
        all_ok = all_ok and ok
    if not results:
        return {"ok": None, "detail": "nessun account Google connesso"}
    return {"ok": all_ok, "detail": " · ".join(results)}


def describe_folder(account: str, folder_id: str) -> dict:
    """Name and type of a folder, so an admin CONFIRMS a name, not a 33-char id
    (the confinement API's `_describe_folder`, moved)."""
    from . import gdrive
    out = {"id": folder_id}
    try:
        meta = google_io.call(gdrive._resolve_account(account), "drive", "v3",
                              "GET files/{fileId}", params={
                                  "fileId": folder_id, "fields": "id, name, mimeType",
                                  "supportsAllDrives": True})
        out["name"] = meta.get("name")
        out["is_folder"] = meta.get("mimeType") == "application/vnd.google-apps.folder"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:100]}"
        out["is_folder"] = False
    return out
