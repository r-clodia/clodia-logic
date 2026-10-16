"""Helper OAuth2 Google (stdlib) — google-pack's copy of clodia-tools
`server/google_oauth.py` (clodia-platform#558), used by this component's
consent routes (`routes.py`). The gateway keeps its own for the CLI
(`connect_email.py`) and the `native` position.

Niente segreti hardcoded: il client dell'app (client_id/secret/redirect) vive
nella vault come credenziale `app_google_oauth`. Lo scambio code→refresh token
avviene server-side; il valore non raggiunge mai un modello.
"""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from urllib.request import Request, urlopen

# Gmail (IMAP/SMTP via XOAUTH2) + openid/email per ricavare l'indirizzo
# dell'account scelto anche quando la Gmail API non è abilitata nel progetto.
SCOPE = "https://mail.google.com/ openid https://www.googleapis.com/auth/userinfo.email"
# Scope del connettore "Google Workspace" (Drive · Docs · Calendar). Gmail
# resta un connettore separato col proprio scope. `userinfo.email`+`openid`
# servono solo a ricavare l'email dell'account scelto, senza l'API Gmail.
WORKSPACE_SCOPE = " ".join([
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
])
# Scope UNIFICATO del connettore "Google": Gmail + Drive + Docs + Calendar in un
# solo consenso → un solo refresh token per account (niente due grant che si
# scalzano a vicenda). È l'unione di SCOPE (Gmail) e WORKSPACE_SCOPE.
UNIFIED_SCOPE = " ".join([
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
])
DEFAULT_REDIRECT = "http://127.0.0.1"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# nome canonico della credenziale "client dell'app" nella vault
APP_CREDENTIAL = "app_google_oauth"


def parse_client_file(path: str) -> dict:
    """Estrae {client_id, client_secret, redirect_uri} dal JSON scaricato da
    Google Cloud (annidato sotto `installed`/`web`) o da un JSON piatto."""
    data = json.loads(Path(path).read_text())
    node = data.get("installed") or data.get("web") or data
    uris = node.get("redirect_uris") or []
    return {
        "client_id": node.get("client_id"),
        "client_secret": node.get("client_secret"),
        "redirect_uri": uris[0] if uris else DEFAULT_REDIRECT,
    }


def consent_url(client_id: str, redirect: str, scope: str = SCOPE,
                login_hint: str | None = None, state: str | None = None,
                prompt: str = "consent") -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",   # → refresh token
        # 'select_account consent' → mostra il selettore account E forza il
        # rilascio del refresh token. 'consent' da solo punta all'account
        # già loggato senza far scegliere.
        "prompt": prompt,
    }
    if login_hint:
        params["login_hint"] = login_hint
    if state:
        params["state"] = state
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def get_profile_email(access_token: str) -> str:
    """Indirizzo email dell'account autenticato, dal profilo Gmail.

    Usa l'access token appena ottenuto per chiamare `users.getProfile`
    (coperto dallo scope `https://mail.google.com/`): così l'account viene
    determinato da QUALE casella l'utente ha scelto nel consenso, non da un
    valore inserito prima. Richiede Gmail API abilitata.
    """
    from urllib.error import HTTPError
    req = Request("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                  headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"Gmail API {e.code}: {body or e.reason} "
                           "(la Gmail API è abilitata nel progetto?)") from e
    email = data.get("emailAddress")
    if not email:
        raise RuntimeError("profilo Gmail senza emailAddress")
    return email


def get_userinfo_email(access_token: str) -> str:
    """Email dell'account autenticato dall'endpoint OpenID userinfo.

    Usato dal connettore Google Workspace (scope `userinfo.email`), che NON
    include lo scope Gmail e quindi non può usare `users.getProfile`.
    """
    req = Request("https://www.googleapis.com/oauth2/v2/userinfo",
                  headers={"Authorization": f"Bearer {access_token}"})
    with urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    email = data.get("email")
    if not email:
        raise RuntimeError("userinfo senza email")
    return email


def exchange_code(client_id: str, client_secret: str, code: str, redirect: str) -> dict:
    """Scambia l'authorization code con i token. Ritorna il JSON di Google."""
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect,
    }).encode()
    req = Request(TOKEN_URL, data=body,
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())
