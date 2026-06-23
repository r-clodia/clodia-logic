"""Helper OAuth2 + PKCE per l'abbonamento OpenAI (codex login / ChatGPT).

Reimplementa il flusso di `codex login` (ChatGPT subscription) lato server, così
da poterlo guidare dalla webui come per Anthropic. Le costanti sono quelle reali
del client pubblico della CLI codex (codex-rs v0.135.0), verificate sul binario:
  - client pubblico `app_EMoamEEZ73f0CkXaXp7hrann` (PKCE, nessun secret)
  - issuer https://auth.openai.com, /oauth/authorize + /oauth/token
  - redirect http://localhost:1455/auth/callback (loopback della CLI)
  - header `originator: codex_cli_rs`

UX (manuale): l'utente autorizza su auth.openai.com; il browser viene rediretto
su `localhost:1455/auth/callback?code=…&state=…` (pagina non raggiungibile, non
gira la CLI) → l'utente copia quell'URL (o il solo `code`) e lo incolla.

Lo storage finale ha la forma del `~/.codex/auth.json` di codex (auth_mode
chatgpt, OPENAI_API_KEY null, tokens{id_token,access_token,refresh_token,
account_id}, last_refresh) → riusabile materializzando un CODEX_HOME quando si
cablerà il runtime di ophelia. Nessun valore raggiunge mai il modello.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import secrets
import urllib.parse
from urllib.request import Request, urlopen

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/oauth/authorize"
TOKEN_URL = f"{ISSUER}/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPES = "openid profile email offline_access"
ORIGINATOR = "codex_cli_rs"
USER_AGENT = "clodia-agent-server (oauth provider connect)"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def authorize_url(challenge: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # parametri specifici codex: organizzazioni nel id_token + flow CLI
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": ORIGINATOR,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def _post_token(payload: dict) -> dict:
    """POST form-encoded al token endpoint OpenAI (header originator + UA)."""
    body = urllib.parse.urlencode(payload).encode()
    req = Request(TOKEN_URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "originator": ORIGINATOR,
    })
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _account_id(id_token: str) -> str | None:
    """Estrae chatgpt_account_id dalle claim del id_token (JWT)."""
    try:
        claims = json.loads(_b64url_decode(id_token.split(".")[1]))
    except (ValueError, IndexError, json.JSONDecodeError):
        return None
    auth = claims.get("https://api.openai.com/auth") or {}
    return auth.get("chatgpt_account_id")


def _auth_json(tok: dict) -> dict:
    """Costruisce il bundle nel formato ~/.codex/auth.json (auth_mode chatgpt)."""
    id_token = tok.get("id_token", "")
    return {
        "method": "subscription",          # campo interno providers (non di codex)
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": id_token,
            "access_token": tok.get("access_token"),
            "refresh_token": tok.get("refresh_token"),
            "account_id": _account_id(id_token),
        },
        "last_refresh": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def exchange(code: str, state: str, verifier: str) -> dict:
    """Scambia l'authorization code (PKCE) e ritorna il bundle da persistere
    (forma auth.json di codex)."""
    tok = _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
    })
    if not tok.get("access_token"):
        raise RuntimeError("risposta token senza access_token")
    return _auth_json(tok)


def auth_json_from_stored(stored: dict) -> dict:
    """Estrae il contenuto `~/.codex/auth.json` dal bundle persistito (rimuove
    il campo interno `method` di providers)."""
    return {k: stored[k] for k in ("auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh") if k in stored}


def env_and_refresh(stored: dict) -> tuple[dict, dict | None]:
    """Variabili d'ambiente per il subprocess + eventuale bundle aggiornato.

    L'abbonamento codex non si applica via env ma materializzando un CODEX_HOME
    con auth.json: il wiring del runtime ophelia non è ancora attivo, quindi per
    ora non inietta nulla (il token resta custodito). Ritorna ({}, None)."""
    return {}, None
