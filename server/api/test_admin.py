"""Self-test F1 (riallineato) — stato bootstrap + gate selettivo + creazione del
primo admin via il normale create_agent (popup), con cert dalla CA. Plain python.

    CLODIA_DATA=$(mktemp -d) python3 -m server.api.test_admin
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import warnings


def main() -> int:
    warnings.filterwarnings("ignore")
    os.environ["CLODIA_DATA"] = tempfile.mkdtemp(prefix="clodia-admin-")
    import importlib
    from server.colony import pki
    importlib.reload(pki)
    from server.agents import loader as _loader
    importlib.reload(_loader)
    from server.api import admin, agent_registry
    importlib.reload(admin)
    importlib.reload(agent_registry)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from fastapi import FastAPI
    from starlette.responses import JSONResponse
    from starlette.testclient import TestClient

    pki.init_ca()
    ok = 0
    fail = 0

    def check(name, cond):
        nonlocal ok, fail
        print(f"  {'✓' if cond else '✗'} {name}")
        ok += cond
        fail += (not cond)

    # 1. stato iniziale: non inizializzato
    check("state uninitialized", admin.is_initialized() is False)

    # 2. gate stretto pre-claim (replica la logica reale: NON si rivela nulla,
    #    solo admin-state + create-superadmin + infra)
    app = FastAPI()

    def _preclaim_allowed(method, path):
        if method in ("HEAD", "OPTIONS"):
            return True
        if method == "GET":
            return (path.startswith("/api/admin") or path == "/health"
                    or path == "/openapi.json" or path.startswith("/docs")
                    or path.startswith("/redoc"))
        if method == "POST":
            return path == "/api/agents"
        return False

    @app.middleware("http")
    async def gate(request, call_next):
        if admin.is_initialized() or _preclaim_allowed(request.method, request.url.path):
            return await call_next(request)
        return JSONResponse({"error": "uninitialized"}, status_code=423)

    @app.get("/api/things")
    async def get_things():
        return {"ok": True}

    @app.get("/api/admin/state")
    async def fake_state():
        return {"ok": True}

    @app.post("/clodia/chats")
    async def new_chat():
        return {"ok": True}

    @app.post("/api/agents")
    async def fake_create():
        return {"ok": True}

    c = TestClient(app)
    check("GET generico BLOCCATO pre-claim (423)", c.get("/api/things").status_code == 423)
    check("GET /api/admin/state permesso pre-claim", c.get("/api/admin/state").status_code == 200)
    check("POST chat bloccato pre-claim (423)", c.post("/clodia/chats").status_code == 423)
    check("POST /api/agents permesso pre-claim", c.post("/api/agents").status_code == 200)

    # 3. crea il PRIMO human via create_agent → cert CA + ruolo superadmin
    bkey = Ed25519PrivateKey.generate()
    pub_pem = bkey.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    body = agent_registry.AgentCreate(name="owner", type="human",
                                      display_name="owner", constitution="none",
                                      pubkey=pub_pem)
    created = asyncio.run(agent_registry.create_agent(body))
    check("primo human → role superadmin", getattr(created, "role", None) == "superadmin")
    check("human senza model (non eseguito)", getattr(created, "model", None) is None)
    check("human senza system_prompt", getattr(created, "system_prompt", None) is None)
    check("cert admin emesso dalla CA", pki.agent_cert_path("owner").is_file())
    check("nessun identity.key server-side", not pki.agent_key_path("owner").is_file())

    # 4. ora inizializzato
    check("state initialized dopo create", admin.is_initialized() is True)

    # 5. il token firmato dal browser autentica via CA (catena completa)
    import json as _j, time as _t
    now = int(_t.time())
    bdy = pki._b64e(_j.dumps({"agent": "owner", "execution_id": "", "iat": now,
                              "exp": now + 600, "aud": pki.TOKEN_AUDIENCE},
                             separators=(",", ":")).encode())
    sig = pki._b64e(bkey.sign(bdy.encode()))
    payload = pki.verify_session_token(f"{pki.TOKEN_PREFIX}.{bdy}.{sig}")
    check("session token owner valido via CA", payload.get("agent") == "owner")

    # 6. un secondo human NON è superadmin (solo il primo reclama)
    bkey2 = Ed25519PrivateKey.generate()
    pub2 = bkey2.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    created2 = asyncio.run(agent_registry.create_agent(
        agent_registry.AgentCreate(name="ospite", type="human", constitution="none", pubkey=pub2)))
    check("secondo human → role admin (non superadmin)", getattr(created2, "role", None) == "admin")

    # 7. login per FIRMA: il token di owner è identificato come 'owner' e NON
    #    verifica contro il cert di 'ospite' (chiave diversa).
    check("verify_token_against riconosce il proprietario",
          pki.verify_token_against(f"{pki.TOKEN_PREFIX}.{bdy}.{sig}", "owner").get("agent") == "owner")
    refused = False
    try:
        pki.verify_token_against(f"{pki.TOKEN_PREFIX}.{bdy}.{sig}", "ospite")
    except PermissionError:
        refused = True
    check("verify_token_against rifiuta un principal con altra chiave", refused)

    print(f"\n{ok} ok, {fail} fail")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
