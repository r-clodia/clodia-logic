"""Token firmati one-time per la decisione dei gate dei workflow.

La notifica (Telegram/email) all'owner porta un link a una pagina SENZA login
(`/gate/{token}`): il token stesso autorizza la decisione. HMAC con chiave
per-istanza (mai esposta). Il token codifica run_id + stage + un NONCE che
deve combaciare con quello salvato sul run → **one-time**: risolto il gate il
nonce viene azzerato e il link muore. TTL lungo (le decisioni possono attendere).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from ..config import data_path

_TTL_DEFAULT = 7 * 24 * 3600      # 7 giorni: un gate può restare in attesa

_KEY_CACHE: bytes | None = None


def _key() -> bytes:
    global _KEY_CACHE
    if _KEY_CACHE is not None:
        return _KEY_CACHE
    kp = data_path("secrets") / "gate-sign.key"
    kp.parent.mkdir(parents=True, exist_ok=True)
    if not kp.is_file():
        kp.write_bytes(os.urandom(32))
        kp.chmod(0o600)
    _KEY_CACHE = kp.read_bytes()
    return _KEY_CACHE


def new_nonce() -> str:
    return secrets.token_hex(8)


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(t: str) -> bytes:
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))


def make(run_id: str, stage_idx: int, nonce: str, ttl: int = _TTL_DEFAULT) -> str:
    """Token workflow = b64(payload).sig. payload = {run, stage, nonce, exp}."""
    return _seal({"run": run_id, "stage": int(stage_idx), "nonce": nonce,
                  "exp": int(time.time()) + ttl})


def make_job(proposal_id: int, nonce: str, ttl: int = _TTL_DEFAULT) -> str:
    """Token per una PROPOSTA DI JOB. payload = {job, nonce, exp}. Stessa firma
    del gate workflow; il `kind` è dato dalla chiave `job` nel payload."""
    return _seal({"job": int(proposal_id), "nonce": nonce,
                  "exp": int(time.time()) + ttl})


def _seal(payload: dict) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def _open(token: str) -> dict | None:
    """Verifica firma + scadenza → payload grezzo, altrimenti None."""
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    good = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(good, sig or ""):
        return None
    try:
        payload = json.loads(_b64d(body))
    except Exception:  # noqa: BLE001
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def verify(token: str) -> dict | None:
    """Ritorna il payload {run, stage, nonce} se firma valida e non scaduto,
    altrimenti None. (Il controllo one-time — nonce == run.gate_nonce — lo fa
    il chiamante contro lo stato del run.)"""
    payload = _open(token)
    if not payload or "run" not in payload:
        return None
    return {"run": str(payload.get("run")), "stage": int(payload.get("stage", -1)),
            "nonce": str(payload.get("nonce"))}


def verify_job(token: str) -> dict | None:
    """Ritorna {job, nonce} se token di proposta job valido, altrimenti None."""
    payload = _open(token)
    if not payload or "job" not in payload:
        return None
    return {"job": int(payload.get("job")), "nonce": str(payload.get("nonce"))}


def token_kind(token: str) -> str | None:
    """'workflow' | 'job' | None — per il routing in gate_public."""
    payload = _open(token)
    if not payload:
        return None
    if "job" in payload:
        return "job"
    if "run" in payload:
        return "workflow"
    return None
