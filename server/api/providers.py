"""Providers — credenziali dei MOTORI DI INFERENZA (Anthropic, OpenAI…).

Opzione B (spec agent-identity-model-spec.md §6): i provider sono consumati da
clodia-logic (è lui che fa inferenza), quindi gestiti qui. Le credenziali sono
custodite per-owner in `CLODIA_DATA/providers/<id>.json` (0600) e applicate
all'ambiente del subprocess agente dal runtime (`session.py`) — **mai esposte al
modello né restituite via API**.

Due meccanismi per provider:
  - **subscription**: login dell'abbonamento via **OAuth+PKCE** (modulo dedicato
    per provider: `anthropic_oauth` su claude.ai → `CLAUDE_CODE_OAUTH_TOKEN`;
    `openai_oauth` = codex login su auth.openai.com → bundle auth.json). Il modulo
    espone un'interfaccia uniforme (`pkce_pair/authorize_url/exchange/
    env_and_refresh`) così questo file resta agnostico dal provider.
  - **apikey**: chiave API → env dedicata (ANTHROPIC_API_KEY / OPENAI_API_KEY).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
import urllib.parse

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import data_path, workspace_path
from . import anthropic_oauth, openai_oauth, provider_store

router = APIRouter()
LOG = logging.getLogger("agent-server.api.providers")

# Fase 4: le credenziali provider NON vivono più su `${CLODIA_DATA}/providers`
# ma nel VAULT del gateway clodia-tools (modello pure-gateway, nessuna copia
# locale). `provider_store` è il backend remoto via ckt1. Login/refresh/env
# injection restano qui sotto; cambia solo il `_read`/`_write`/`unlink`.

# Definizioni provider = DATI clonabili in `providers/` alla root del repo (NON
# credenziali — quelle nel vault del gateway). Ogni `providers/<id>.yaml` dichiara
# name, apikey_env, sdk (di default) e flow (quale flusso OAuth lo implementa).
PROVIDERS_DEF_DIR = workspace_path("providers")

# Registry dei flussi OAuth+PKCE: codice provider-specifico (resta in server/),
# interfaccia uniforme (pkce_pair/authorize_url/exchange/env_and_refresh). La
# definizione-dato lega ogni provider al suo flow per `flow:`.
_OAUTH_FLOWS = {
    "anthropic": anthropic_oauth,
    "openai": openai_oauth,
}


# Alias dei vecchi id provider (pre-split apikey/abbonamento, 21 giu 2026) verso
# i nuovi. Back-compat per `provider:` negli agent.yaml e per credenziali legacy:
# l'API era il percorso commerciale di default, quindi mappa lì.
PROVIDER_ALIASES = {
    "anthropic": "anthropic-api",
    "openai": "openai-api",
}


def _load_catalog() -> tuple[dict, dict]:
    """Carica il catalogo provider dalle definizioni in `providers/<id>.yaml`.
    Ritorna (catalog, sdk_providers). `catalog[id]` = {name, apikey_env, oauth,
    mechanism, sdk, priority}. `sdk_providers[sdk]` = lista id ordinata per
    priority (preferenza: più basso prima)."""
    catalog: dict[str, dict] = {}
    by_sdk: dict[str, list[tuple[int, str]]] = {}
    if PROVIDERS_DEF_DIR.is_dir():
        for f in sorted(PROVIDERS_DEF_DIR.glob("*.yaml")):
            d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            pid = f.stem
            prio = int(d.get("priority", 100))
            catalog[pid] = {
                "name": d.get("name", pid.capitalize()),
                "apikey_env": d.get("apikey_env"),
                "oauth": _OAUTH_FLOWS.get(d.get("flow")),
                # meccanismo unico per provider (split DPA/costi): apikey | subscription.
                "mechanism": d.get("mechanism") or ("subscription" if d.get("flow") else "apikey"),
                "sdk": d.get("sdk"),
                "priority": prio,
                # Env STATICHE aggiuntive (es. Bedrock: CLAUDE_CODE_USE_BEDROCK, AWS_REGION,
                # model id EU) iniettate quando il provider è effettivo, oltre alla apikey.
                "extra_env": dict(d.get("extra_env") or {}),
                # Classificazione di sovranità (SEAL + SOV) per la UI e i guard tier.
                "sovereignty": dict(d.get("sovereignty") or {}),
            }
            if d.get("sdk"):
                by_sdk.setdefault(d["sdk"], []).append((prio, pid))
    sdk_providers = {sdk: [pid for _, pid in sorted(lst)] for sdk, lst in by_sdk.items()}
    return catalog, sdk_providers


# id → metadati. Provider compatibili di default per SDK (ordinati per priority):
# claude→[anthropic-api, claude-pro-max], codex→[openai-api, codex]. Tutto derivato
# dai dati in providers/.
_CATALOG, SDK_PROVIDERS = _load_catalog()


def _normalize(pid: str | None) -> str | None:
    """Risolve gli alias dei vecchi id provider verso i nuovi."""
    if pid is None:
        return None
    return PROVIDER_ALIASES.get(pid, pid)


def default_providers_for_sdk(agent_sdk: str | None) -> list[str]:
    """Provider compatibili di default per un SDK, ordinati per preferenza."""
    return list(SDK_PROVIDERS.get(agent_sdk or "claude", []))


def candidate_providers(providers: list[str] | None, provider: str | None,
                        agent_sdk: str | None) -> list[str]:
    """Lista ordinata di provider compatibili per un agent (ordine = preferenza):
    - `providers` esplicito (lista nel seed) ha priorità;
    - back-compat: `provider` singolo → lista a un elemento;
    - fallback: default dell'SDK (API prima, abbonamento poi).
    Gli id ignoti al catalogo sono scartati; gli alias legacy sono normalizzati."""
    if providers:
        cands = [_normalize(p) for p in providers]
    elif provider:
        cands = [_normalize(provider)]
    else:
        cands = default_providers_for_sdk(agent_sdk)
    # dedup preservando l'ordine, solo id noti al catalogo
    seen: set[str] = set()
    out: list[str] = []
    for p in cands:
        if p and p in _CATALOG and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def effective_provider(providers: list[str] | None, provider: str | None,
                       agent_sdk: str | None, connected: set[str]) -> str | None:
    """Primo provider compatibile che risulta collegato, o None (→ agent
    disabilitato: nessun provider compatibile è collegato)."""
    for p in candidate_providers(providers, provider, agent_sdk):
        if p in connected:
            return p
    return None


def resolve_provider(provider: str | None, agent_sdk: str | None) -> str | None:
    """DEPRECATO (pre-lista): primo candidato compatibile, senza guardare lo
    stato di connessione. Mantenuto per i chiamanti non ancora migrati."""
    cands = candidate_providers(None, provider, agent_sdk)
    return cands[0] if cands else None


def _bundle_usable(pid: str, d: dict | None) -> bool:
    """Una credenziale è USABILE (→ provider davvero 'collegato') se produce
    auth: apikey con `api_key` non vuota, o subscription con bundle valido. Una
    voce vuota/parziale nel vault (es. method=None) NON conta come collegata,
    altrimenti un provider rotto verrebbe scelto come effettivo e l'agent non
    autenticherebbe ('Not logged in')."""
    if not d:
        return False
    meta = _CATALOG.get(pid)
    if not meta:
        return False
    if meta["mechanism"] == "apikey":
        return bool(d.get("api_key"))
    return d.get("method") == "subscription"


def connected_provider_ids() -> set[str]:
    """ID dei provider collegati CON credenziale usabile (vedi `_bundle_usable`).

    Legge il bundle di ogni provider noto (alias legacy normalizzati). Su gateway
    irraggiungibile rilancia: i chiamati a valle (enforcement) sono fail-open."""
    present = {_normalize(p) for p in provider_store.list_ids()}
    return {p for p in present if p in _CATALOG and _bundle_usable(p, _read(p))}


# state → {verifier, exp}. In memoria: il login è una sessione breve. Anti-CSRF
# + binding del PKCE verifier allo state restituito dalla pagina di consenso.
_login_states: dict[str, dict] = {}
_STATE_TTL = 600  # 10 min


def _read(pid: str) -> dict | None:
    """Bundle della credenziale dal vault del gateway, o None se assente.

    Degrada a None (con log) se il gateway è irraggiungibile: a valle equivale a
    'provider non collegato' (l'agente non parte con un messaggio chiaro), che è
    il comportamento fail-safe voluto."""
    try:
        return provider_store.read(pid)
    except provider_store.ProviderStoreError as e:
        LOG.error("provider_store.read(%s) fallita: %s", pid, e)
        return None


def _write(pid: str, data: dict) -> None:
    """Persiste il bundle nel vault del gateway. Rilancia su errore (il
    login/refresh che chiama deve poterlo segnalare)."""
    provider_store.write(pid, data)


def _gc_states() -> None:
    now = time.time()
    for k in [k for k, v in _login_states.items() if v["exp"] < now]:
        _login_states.pop(k, None)


def _parse_code_state(raw: str) -> tuple[str, str | None]:
    """Estrae (code, state) dalla stringa incollata dall'utente, in tre forme:
    URL di redirect (`...?code=X&state=Y`), `code#state`, o solo `code`."""
    raw = raw.strip()
    if "code=" in raw:  # URL completo o query string
        q = urllib.parse.urlparse(raw).query or raw.split("?", 1)[-1]
        params = urllib.parse.parse_qs(q)
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [None])[0]
        return code, state
    if "#" in raw:
        code, _, state = raw.partition("#")
        return code, (state or None)
    return raw, None


@router.get("/api/providers")
async def list_providers() -> dict:
    """Stato dei provider (mai i valori delle credenziali)."""
    out = []
    for pid, meta in _CATALOG.items():
        d = _read(pid)
        out.append({
            "id": pid, "name": meta["name"],
            "connected": _bundle_usable(pid, d), "via": (d or {}).get("method"),
            # meccanismo UNICO del provider (split DPA/costi): apikey | subscription.
            "mechanism": meta["mechanism"],
            "sdk": meta.get("sdk"),
            # capacità: il provider supporta il login-abbonamento OAuth-paste?
            "subscription": "oauth" if meta.get("oauth") else None,
            # sovranità: livello SEAL effettivo (+ dettaglio) per la UI / guard tier.
            "seal": (meta.get("sovereignty") or {}).get("seal"),
            "sovereignty": meta.get("sovereignty") or None,
        })
    return {"providers": out}


class KeyBody(BaseModel):
    api_key: str


@router.post("/api/providers/{pid}/key")
async def set_key(pid: str, body: KeyBody) -> dict:
    if pid not in _CATALOG:
        raise HTTPException(404, f"provider sconosciuto: {pid}")
    if not body.api_key.strip():
        raise HTTPException(400, "api_key vuota")
    try:
        _write(pid, {"method": "apikey", "api_key": body.api_key.strip()})
    except provider_store.ProviderStoreError as e:
        raise HTTPException(502, f"salvataggio sul gateway fallito: {str(e)[:160]}")
    return {"connected": True, "via": "apikey"}


@router.post("/api/providers/{pid}/login/start")
async def login_start(pid: str) -> dict:
    """Avvia il login-abbonamento OAuth: genera PKCE verifier+state e restituisce
    l'authorize URL del provider. L'utente lo apre, autorizza con il proprio
    abbonamento e ottiene il code da incollare in /login/complete."""
    meta = _CATALOG.get(pid)
    if meta is None:
        raise HTTPException(404, f"provider sconosciuto: {pid}")
    oauth = meta.get("oauth")
    if oauth is None:
        raise HTTPException(400, f"login-abbonamento non disponibile per {pid}")
    _gc_states()
    verifier, challenge = oauth.pkce_pair()
    state = secrets.token_urlsafe(24)
    _login_states[state] = {"verifier": verifier, "exp": time.time() + _STATE_TTL}
    return {"auth_url": oauth.authorize_url(challenge, state), "state": state}


class CodeBody(BaseModel):
    code: str   # `code#state`, URL di redirect, o solo `code`


@router.post("/api/providers/{pid}/login/complete")
async def login_complete(pid: str, body: CodeBody) -> dict:
    """Completa il login-abbonamento: l'utente incolla il code ottenuto dopo
    l'autorizzazione. Exchange PKCE server-side, si persiste il bundle (mai
    esposto al modello)."""
    meta = _CATALOG.get(pid)
    if meta is None:
        raise HTTPException(404, f"provider sconosciuto: {pid}")
    oauth = meta.get("oauth")
    if oauth is None:
        raise HTTPException(400, f"login-abbonamento non disponibile per {pid}")
    if not body.code.strip():
        raise HTTPException(400, "code vuoto")
    code, state = _parse_code_state(body.code)
    if not code:
        raise HTTPException(400, "code non riconosciuto nella stringa incollata")
    # Lo state lega il PKCE verifier alla sessione di login (anti-CSRF). Se la
    # stringa incollata non lo riporta ma c'è un unico login in volo, lo si usa.
    if state is None and len(_login_states) == 1:
        state = next(iter(_login_states))
    st = _login_states.pop(state, None) if state else None
    if st is None:
        raise HTTPException(400, "state invalido o scaduto — riavvia il login")
    try:
        stored = oauth.exchange(code, state, st["verifier"])
    except Exception as e:  # noqa: BLE001 — superficie esterna, niente dettagli al modello
        raise HTTPException(502, f"exchange fallito: {str(e)[:160]}")
    try:
        _write(pid, stored)
    except provider_store.ProviderStoreError as e:
        raise HTTPException(502, f"salvataggio sul gateway fallito: {str(e)[:160]}")
    return {"connected": True, "via": "subscription"}


@router.delete("/api/providers/{pid}")
async def disconnect(pid: str) -> dict:
    try:
        provider_store.delete(pid)
    except provider_store.ProviderStoreError as e:
        raise HTTPException(502, f"disconnect fallito sul gateway: {str(e)[:160]}")
    if pid == "codex":
        # rimuovi anche il CODEX_HOME materializzato (auth.json del runtime codex)
        ah = CODEX_HOME_DIR / "auth.json"
        if ah.is_file():
            ah.unlink()
    return {"connected": False}


# CODEX_HOME materializzato dal bundle abbonamento OpenAI: ospita auth.json (e
# i file di sessione che codex scrive). Persistente (sotto data/) così il
# refresh-token che codex rinnova in-place non si perde ai recreate.
CODEX_HOME_DIR = data_path("data/codex-home")


def codex_home() -> "os.PathLike | None":
    """Path di un CODEX_HOME pronto all'uso se l'abbonamento OpenAI è connesso,
    altrimenti None. Scrive auth.json (0600) la prima volta; se esiste già lo
    lascia a codex, che vi rinnova il token in autonomia."""
    d = _read("codex")
    if not d or d.get("method") != "subscription":
        return None
    CODEX_HOME_DIR.mkdir(parents=True, exist_ok=True)
    auth_path = CODEX_HOME_DIR / "auth.json"
    if not auth_path.is_file():
        auth_path.write_text(json.dumps(openai_oauth.auth_json_from_stored(d)))
        os.chmod(auth_path, 0o600)
    return CODEX_HOME_DIR


def provider_env(pid: str | None = None) -> dict[str, str]:
    """Variabili d'ambiente da iniettare nel subprocess agente a partire dalle
    credenziali provider salvate. Usato dal runtime (session.py). Il valore non
    transita mai dal modello.

    Con `pid` inietta SOLO quel provider (il provider effettivo dell'agent): con
    4 provider distinti, due credenziali dello stesso SDK collegate insieme (es.
    anthropic-api + claude-pro-max) si escluderebbero a vicenda. Senza `pid`
    (back-compat) inietta tutti i provider collegati."""
    env: dict[str, str] = {}
    pids = [_normalize(pid)] if pid else list(_CATALOG)
    for pid in pids:  # noqa: PLR1704 — riuso volutamente il nome
        meta = _CATALOG.get(pid)
        if not meta:
            continue
        d = _read(pid)
        if not d:
            continue
        if d.get("method") == "subscription" and meta.get("oauth"):
            sub_env, new_stored = meta["oauth"].env_and_refresh(d)
            if new_stored:
                # Writeback del token rinnovato nel vault. Non-fatale: se il
                # gateway non lo persiste ora, la sessione usa comunque il token
                # appena rinnovato in memoria; si riproverà al prossimo start.
                try:
                    _write(pid, new_stored)
                except provider_store.ProviderStoreError as e:
                    LOG.warning("writeback refresh provider '%s' non persistito: %s", pid, e)
            env.update(sub_env)
        elif d.get("method") == "apikey" and d.get("api_key"):
            env[meta["apikey_env"]] = d["api_key"]
            # Env statiche del provider (es. Bedrock: flag + region + model id EU).
            env.update(meta.get("extra_env") or {})
    return env


def all_provider_env_keys() -> set[str]:
    """Tutti i nomi di env-var che un provider POTREBBE iniettare nel subprocess:
    le `apikey_env` del catalogo (es. ANTHROPIC_API_KEY, OPENAI_API_KEY) + l'env
    delle subscription OAuth (CLAUDE_CODE_OAUTH_TOKEN). Usato dal runtime per
    azzerare le credenziali dei provider NON effettivi prima di iniettare quella
    scelta (mutua esclusione: una chiave globale/residua non deve oscurare un
    agent assegnato a un altro provider)."""
    keys: set[str] = set()
    for meta in _CATALOG.values():
        k = meta.get("apikey_env")
        if k:
            keys.add(k)
        # Anche le env statiche (es. CLAUDE_CODE_USE_BEDROCK, AWS_REGION, model id):
        # vanno azzerate quando l'agent usa un ALTRO provider, altrimenti un residuo
        # Bedrock dirotterebbe un agent assegnato all'API diretta. Mutua esclusione.
        keys.update((meta.get("extra_env") or {}).keys())
    keys.add(anthropic_oauth.SUBSCRIPTION_ENV)
    return keys
