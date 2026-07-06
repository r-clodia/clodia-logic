"""URL firmati a scadenza per il download dei file (fix sicurezza 7 lug 2026).

I link <a> del browser non portano l'Authorization header: senza firma, un
endpoint di download o è aperto a chiunque abbia il link (la vulnerabilità
trovata da Davide) o è inutilizzabile dai link. Soluzione standard: URL
presigned — la webui (autenticata) chiede la firma, il link vale TTL breve
e SOLO per quell'esatto file. Chiave HMAC per-istanza nella datadir (mai
esposta; il valore non transita dal modello né dalle API).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

from ..config import data_path

_TTL_DEFAULT = 900  # 15 min: rende il link condiviso inutile quasi subito

_KEY_CACHE: bytes | None = None


def _key() -> bytes:
    global _KEY_CACHE
    if _KEY_CACHE is not None:
        return _KEY_CACHE
    kp = data_path("secrets") / "download-sign.key"
    kp.parent.mkdir(parents=True, exist_ok=True)
    if not kp.is_file():
        kp.write_bytes(os.urandom(32))
        kp.chmod(0o600)
    _KEY_CACHE = kp.read_bytes()
    return _KEY_CACHE


def _mac(scope: str, exp: int) -> str:
    msg = f"{scope}|{exp}".encode()
    return hmac.new(_key(), msg, hashlib.sha256).hexdigest()


def make(scope: str, ttl: int = _TTL_DEFAULT) -> tuple[int, str]:
    """(exp, sig) per lo scope dato. Scope = identità esatta della risorsa,
    es. "topic|SEAL-2|pratica-x|files/report.pdf"."""
    exp = int(time.time()) + ttl
    return exp, _mac(scope, exp)


def verify(scope: str, exp: int, sig: str) -> bool:
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(_mac(scope, exp), sig or "")
