"""Helper OAuth2 + PKCE per l'abbonamento Anthropic (Claude Max).

Replica il flusso di `claude setup-token`: login dell'abbonamento su claude.ai
→ authorization code → exchange server-side → bundle `claudeAiOauth`
(access_token + refresh_token + scadenza). L'access token si applica poi al
subprocess agente come env `CLAUDE_CODE_OAUTH_TOKEN` (vedi providers.py), così
la Clodia del container fa inferenza con l'abbonamento e non a consumo API.

Niente segreti hardcoded di owner: il client OAuth di Claude Code è **pubblico**
(public client, PKCE senza client_secret) — è lo stesso usato dalla CLI. Lo
scambio code→token avviene server-side; nessun valore raggiunge il modello.

Flusso (manuale, console-callback — coerente con la UX Gmail già rilasciata):
  1. start    → genera (verifier, state), costruisce l'authorize URL claude.ai
                con `code=true` (la pagina di consenso mostra il `code#state`
                da copiare) + code_challenge S256.
  2. l'utente autorizza su claude.ai e copia la stringa `code#state`.
  3. complete → split su '#', exchange con il verifier → bundle token.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from urllib.request import Request, urlopen

# env del subprocess agente per l'abbonamento + soglia di refresh anticipato.
SUBSCRIPTION_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
_REFRESH_SKEW = 300  # 5 min

# Public client di Claude Code (PKCE, nessun secret). Stesso del flusso CLI.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
# Endpoint token dell'abbonamento (Claude Max): è su claude.ai, NON su
# console.anthropic.com (che è il flusso Console/API → rotta /v1/oauth/token
# inesistente lì → 404).
TOKEN_URL = "https://claude.ai/v1/oauth/token"
# Callback "manuale": dopo il consenso la pagina mostra `code#state` da copiare.
REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
SCOPES = "org:create_api_key user:profile user:inference"
# Lo User-Agent serve a superare il WAF davanti a claude.ai: senza, lo UA di
# default `Python-urllib/*` viene bloccato con 403 Forbidden.
USER_AGENT = "clodia-agent-server (oauth provider connect)"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) — challenge = base64url(sha256(verifier))."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def authorize_url(challenge: str, state: str) -> str:
    params = {
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def _post_token(payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = Request(TOKEN_URL, data=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def exchange_code(code: str, state: str, verifier: str) -> dict:
    """Scambia l'authorization code (PKCE) con i token. Ritorna il JSON Anthropic
    ({access_token, refresh_token, expires_in, ...})."""
    return _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "state": state,
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    })


def refresh(refresh_token: str) -> dict:
    """Rinnova l'access token a partire dal refresh token."""
    return _post_token({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    })


# ── interfaccia uniforme usata da providers.py ──────────────────────────────

def exchange(code: str, state: str, verifier: str) -> dict:
    """Scambia il code (PKCE) e ritorna il bundle da persistere."""
    tok = exchange_code(code, state, verifier)
    access = tok.get("access_token")
    if not access:
        raise RuntimeError("risposta token senza access_token")
    return {
        "method": "subscription",
        "access_token": access,
        "refresh_token": tok.get("refresh_token"),
        "expires_at": time.time() + tok["expires_in"] if tok.get("expires_in") else None,
    }


def env_and_refresh(stored: dict) -> tuple[dict, dict | None]:
    """(env da iniettare, bundle aggiornato|None). Rinnova l'access token via
    refresh token se scaduto/in scadenza."""
    access = stored.get("access_token")
    exp = stored.get("expires_at")
    rtok = stored.get("refresh_token")
    new_stored = None
    if exp is not None and rtok and exp - _REFRESH_SKEW <= time.time():
        try:
            tok = refresh(rtok)
            access = tok.get("access_token", access)
            new_stored = {
                "method": "subscription",
                "access_token": access,
                "refresh_token": tok.get("refresh_token", rtok),
                "expires_at": time.time() + tok["expires_in"] if tok.get("expires_in") else None,
            }
        except Exception:  # noqa: BLE001 — usa il token corrente, l'agente fallirà chiaro
            pass
    return ({SUBSCRIPTION_ENV: access} if access else {}), new_stored
