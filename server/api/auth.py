"""Auth API — login OAuth via Claude CLI subprocess.

Flusso:
  POST /auth/login  → avvia `claude auth login --claudeai`, cattura l'URL OAuth,
                       lo restituisce al frontend (il processo resta in attesa del codice)
  POST /auth/code   → invia il codice di ritorno al processo in attesa (stdin)
  GET  /auth/status → verifica se il login è completato (token presente)
  POST /auth/logout → rimuove il token locale
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

LOG = logging.getLogger("auth")
router = APIRouter(prefix="/auth", tags=["auth"])

# Processo di login in corso (uno alla volta)
_login_proc: asyncio.subprocess.Process | None = None
_login_url: str | None = None
_login_error: str | None = None
_login_done: bool = False

# Cache del check `_credentials_exist()`: il root layout della SPA chiama
# `/auth/status` ad ogni mount + navigazione, e il check stat-a tutta la dir
# `~/.claude/` (bind mount Docker). Il check cold prende 10-20s perché
# triggera il populate della filesystem-cache del mount.
#
# Strategia in 2 layer:
# 1. **TTL fresh (10 min)**: entro la TTL la cache è considerata fresca e
#    restituita subito senza ricontrollare il filesystem.
# 2. **Stale-while-revalidate**: scaduta la TTL, restituiamo COMUNQUE
#    l'ultimo valore noto (`result`) senza far aspettare il chiamante, e
#    schediliamo un refresh in background che aggiornerà la cache per il
#    prossimo hit. L'UI non vede mai il blip 20s, salvo il primissimo hit
#    pre-prewarm (coperto dal lifespan startup in main.py).
#
# Login/logout esplicito invalida la cache (chiamata sincrona la prossima volta).
_CRED_CACHE: dict[str, float | bool] = {"ts": 0.0, "result": False, "warm": False}
_CRED_CACHE_TTL = 600.0  # 10 min
_cred_refresh_lock = asyncio.Lock()


def _check_credentials_disk() -> bool:
    """Implementazione concreta del check filesystem. Costoso a freddo
    (bind mount cold). Non usa la cache."""
    claude_dir = Path.home() / ".claude"
    if not claude_dir.is_dir():
        return False
    candidates = [
        claude_dir / "credentials.json",
        claude_dir / ".credentials",
    ]
    for p in candidates:
        if p.exists():
            return True
    skip = {
        "settings.json", "settings.local.json",
        "mcp-needs-auth-cache.json", "policy-limits.json", "stats-cache.json",
    }
    try:
        return any(
            f.suffix == ".json" and f.name not in skip
            for f in claude_dir.iterdir()
            if f.is_file()
        )
    except OSError as e:
        LOG.warning("iterdir su %s fallito: %s", claude_dir, e)
        return False


def _credentials_exist() -> bool:
    """True se claude ha un token OAuth valido. Cached con stale-while-revalidate.

    Chiamato sincrono al boot (prewarm) e ad ogni `/auth/status`. Non blocca
    mai oltre il primo hit pre-prewarm.
    """
    now = time.time()
    age = now - _CRED_CACHE["ts"]

    # Cache mai popolata: chiamata sincrona (1 sola volta, dal prewarm boot)
    if not _CRED_CACHE["warm"]:
        result = _check_credentials_disk()
        _CRED_CACHE["ts"] = now
        _CRED_CACHE["result"] = result
        _CRED_CACHE["warm"] = True
        return result

    # Cache fresca: ritorna subito
    if age < _CRED_CACHE_TTL:
        return bool(_CRED_CACHE["result"])

    # Cache stale: ritorna last_known + scheduling refresh in background
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_refresh_creds_cache())
    except RuntimeError:
        # Nessun event loop (chiamata da sync code): sync refresh fallback
        _CRED_CACHE["ts"] = now
        _CRED_CACHE["result"] = _check_credentials_disk()
    return bool(_CRED_CACHE["result"])


async def _refresh_creds_cache() -> None:
    """Refresh background della cache. Idempotente via lock."""
    if _cred_refresh_lock.locked():
        return
    async with _cred_refresh_lock:
        result = await asyncio.to_thread(_check_credentials_disk)
        _CRED_CACHE["ts"] = time.time()
        _CRED_CACHE["result"] = result


def _invalidate_creds_cache() -> None:
    """Forza il re-check al prossimo hit (chiamato da login/logout)."""
    _CRED_CACHE["warm"] = False
    _CRED_CACHE["ts"] = 0.0


async def _run_login() -> None:
    global _login_proc, _login_url, _login_error, _login_done

    claude_bin = shutil.which("claude")
    if not claude_bin:
        _login_error = "claude CLI non trovato nel PATH"
        return

    # Rimuove ANTHROPIC_API_KEY dall'env: con la chiave API impostata
    # claude usa la modalità API key e rifiuta il flusso OAuth.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

    try:
        _login_proc = await asyncio.create_subprocess_exec(
            claude_bin, "auth", "login", "--claudeai",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        # Legge l'output cercando l'URL OAuth (max 30s)
        url_pattern = re.compile(r"https://\S+")
        deadline = asyncio.get_event_loop().time() + 30

        while asyncio.get_event_loop().time() < deadline:
            try:
                line = await asyncio.wait_for(_login_proc.stdout.readline(), timeout=2.0)
            except asyncio.TimeoutError:
                if _login_proc.returncode is not None:
                    break
                continue
            if not line:
                break
            text = line.decode(errors="replace").strip()
            LOG.info("[claude auth login] %s", text)
            if m := url_pattern.search(text):
                _login_url = m.group(0)
                LOG.info("URL OAuth trovato: %s", _login_url)
                break

        if not _login_url:
            _login_error = "URL OAuth non trovato nell'output di claude auth login"
            if _login_proc.returncode is None:
                _login_proc.terminate()
            return

        # Il processo resta in attesa del codice (stdin). Aspetta max 10 min.
        try:
            await asyncio.wait_for(_login_proc.wait(), timeout=600)
            _login_done = (_login_proc.returncode == 0)
            if not _login_done:
                _login_error = f"claude auth login uscito con codice {_login_proc.returncode}"
        except asyncio.TimeoutError:
            _login_proc.terminate()
            _login_error = "Timeout — codice OAuth non ricevuto entro 10 minuti"

    except Exception as e:
        _login_error = str(e)
        LOG.exception("Errore durante claude auth login")
    finally:
        _login_proc = None


@router.post("/login")
async def start_login():
    global _login_url, _login_error, _login_done

    if _credentials_exist():
        return {"status": "already_logged_in", "url": None}

    if _login_proc is not None and _login_url:
        return {"status": "in_progress", "url": _login_url}

    _login_url = None
    _login_error = None
    _login_done = False

    asyncio.create_task(_run_login())

    # Aspetta fino a 15s per trovare l'URL
    for _ in range(30):
        await asyncio.sleep(0.5)
        if _login_url:
            return {"status": "pending", "url": _login_url}
        if _login_error:
            raise HTTPException(status_code=500, detail=_login_error)

    raise HTTPException(status_code=504, detail="Timeout: URL OAuth non trovato entro 15s")


class CodeBody(BaseModel):
    code: str


@router.post("/code")
async def submit_code(body: CodeBody):
    """Invia il codice di ritorno OAuth al processo claude auth login in attesa."""
    if _login_proc is None or _login_proc.stdin is None:
        raise HTTPException(status_code=409, detail="Nessun login in corso")
    try:
        _login_proc.stdin.write((body.code.strip() + "\n").encode())
        await _login_proc.stdin.drain()
        return {"status": "code_sent"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
async def auth_status():
    return {
        "logged_in": _credentials_exist(),
        "login_in_progress": _login_proc is not None,
        "login_url": _login_url if not _credentials_exist() else None,
        "login_error": _login_error,
    }


@router.post("/logout")
async def logout():
    global _login_url, _login_error, _login_done
    if _login_proc is not None and _login_proc.returncode is None:
        _login_proc.terminate()
    _login_url = None
    _login_error = None
    _login_done = False

    cred = next(
        (p for p in [
            Path.home() / ".claude" / "credentials.json",
            Path.home() / ".claude" / ".credentials",
        ] if p.exists()),
        None
    )
    if cred:
        cred.unlink()
        _invalidate_creds_cache()
        return {"status": "logged_out"}
    return {"status": "already_logged_out"}
