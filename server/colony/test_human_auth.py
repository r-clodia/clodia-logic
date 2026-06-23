"""Self-test catena auth umana (F1): pubkey esterna → cert CA → token firmato
col privkey del 'browser' → verify_session_token. Plain python.

    CLODIA_DATA=$(mktemp -d) python3 -m server.colony.test_human_auth
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import warnings


def main() -> int:
    warnings.filterwarnings("ignore")
    os.environ.setdefault("CLODIA_DATA", tempfile.mkdtemp(prefix="clodia-humanauth-"))
    import importlib
    from server.colony import pki
    importlib.reload(pki)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    ok = 0
    fail = 0

    def check(name, cond):
        nonlocal ok, fail
        print(f"  {'✓' if cond else '✗'} {name}")
        ok += cond
        fail += (not cond)

    pki.init_ca()
    check("CA inizializzata", pki.ca_initialized())

    # il "browser" genera il keypair (dalla masterkey, qui random) e manda la pubkey
    browser_key = Ed25519PrivateKey.generate()
    pub_pem = browser_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    # il server emette il cert per quella pubkey (NON vede la privkey)
    pki.issue_cert_for_pubkey("admin", pub_pem)
    check("cert admin emesso", pki.agent_cert_path("admin").is_file())
    check("nessun identity.key server-side", not pki.agent_key_path("admin").is_file())

    # il browser FIRMA un session token con scadenza (formato ckt1)
    def sign_token(priv, agent, ttl=3600):
        now = int(time.time())
        payload = {"agent": agent, "execution_id": "", "iat": now,
                   "exp": now + ttl, "aud": pki.TOKEN_AUDIENCE}
        body = pki._b64e(json.dumps(payload, separators=(",", ":")).encode())
        sig = pki._b64e(priv.sign(body.encode()))
        return f"{pki.TOKEN_PREFIX}.{body}.{sig}"

    tok = sign_token(browser_key, "admin")
    payload = pki.verify_session_token(tok)
    check("token admin verificato dalla CA", payload.get("agent") == "admin")

    # token firmato da una chiave NON certificata → rifiutato
    intruder = Ed25519PrivateKey.generate()
    try:
        pki.verify_session_token(sign_token(intruder, "admin"))
        check("intruso rifiutato", False)
    except PermissionError:
        check("intruso rifiutato", True)

    # token scaduto → rifiutato
    try:
        pki.verify_session_token(sign_token(browser_key, "admin", ttl=-10))
        check("token scaduto rifiutato", False)
    except PermissionError:
        check("token scaduto rifiutato", True)

    # revoca del principal → token cade
    pki.revoke("admin")
    try:
        pki.verify_session_token(sign_token(browser_key, "admin"))
        check("revoca admin → token cade", False)
    except PermissionError:
        check("revoca admin → token cade", True)

    print(f"\n{ok} ok, {fail} fail")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
