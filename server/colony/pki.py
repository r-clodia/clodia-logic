"""PKI della colonia (decisione owner 12 giu 2026).

Clodia è la **CA del sistema**: ogni agente, alla creazione del seed,
riceve una coppia di chiavi ed25519 e un certificato X.509 firmato dalla
CA. L'identità diventa proprietà crittografica del seed (Terza Legge:
patrimonio genetico verificabile, portabile su più macchine).

Layout:
- ``CLODIA_DATA/secrets/ca/ca.key``            — chiave CA (0600, usata solo a seed/revoca)
- ``CLODIA_DATA/secrets/ca/ca.crt``            — certificato CA (pubblico)
- ``CLODIA_DATA/secrets/agents/<n>/identity.key`` — chiave privata agente (area runner,
                                                   MAI montata nel workspace)
- ``CLODIA_DATA/pki/certs/<n>.crt``            — certificati pubblici (registry)
- ``CLODIA_DATA/pki/revoked.json``             — revoche (CRL minimale)

Sessioni: allo spawn di una execution il **runner** firma con la chiave
privata dell'agente un token corto ``ckt1.<b64 payload>.<b64 firma>``
(payload: agent, execution_id, iat, exp, aud). Nel workspace entra SOLO
il token: la chiave privata non è mai esposta al modello. Il keystore
valida firma → certificato → catena CA → revoche → scadenza.

CLI (init una tantum / retrofit):
    python3 -m server.colony.pki init-ca
    python3 -m server.colony.pki issue <agent> | issue-all
    python3 -m server.colony.pki revoke <agent>
    python3 -m server.colony.pki status
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.x509.oid import NameOID

from ..config import data_path

LOG = logging.getLogger("agent-server.colony.pki")

CA_DIR = data_path("secrets") / "ca"
CA_KEY = CA_DIR / "ca.key"
CA_CRT = CA_DIR / "ca.crt"
AGENT_SECRETS = data_path("secrets") / "agents"
PKI_DIR = data_path("pki")
CERTS_DIR = PKI_DIR / "certs"
REVOKED_FILE = PKI_DIR / "revoked.json"

CA_COMMON_NAME = "Clodia Colony CA"
COLONY_ORG = "clodia-colony"
CERT_DAYS = 365 * 3
SESSION_TTL_SECONDS = 45 * 60  # ≥ RUNNER_MAX_SECONDS (30 min) + margine
TOKEN_PREFIX = "ckt1"
TOKEN_AUDIENCE = "keystore"


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _write_private(path: Path, key: Ed25519PrivateKey) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    os.chmod(path, 0o600)


def _load_private(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path}: attesa chiave ed25519")
    return key


# ── CA ───────────────────────────────────────────────────────────────


def ca_initialized() -> bool:
    return CA_KEY.is_file() and CA_CRT.is_file()


def init_ca(force: bool = False) -> Path:
    """Crea la CA della colonia (idempotente salvo force)."""
    if ca_initialized() and not force:
        return CA_CRT
    key = Ed25519PrivateKey.generate()
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, COLONY_ORG),
    ])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=CERT_DAYS * 2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, algorithm=None))
    _write_private(CA_KEY, key)
    CA_CRT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    LOG.warning("CA colonia inizializzata: %s", CA_CRT)
    return CA_CRT


def _load_ca() -> tuple[Ed25519PrivateKey, x509.Certificate]:
    if not ca_initialized():
        raise RuntimeError("CA non inizializzata: eseguire `pki init-ca`")
    return _load_private(CA_KEY), x509.load_pem_x509_certificate(CA_CRT.read_bytes())


# ── Identità agente ──────────────────────────────────────────────────


def agent_key_path(agent: str) -> Path:
    return AGENT_SECRETS / agent / "identity.key"


def agent_cert_path(agent: str) -> Path:
    return CERTS_DIR / f"{agent}.crt"


def issue_agent_identity(agent: str, force: bool = False) -> Path:
    """Genera keypair + certificato firmato dalla CA per l'agente."""
    if agent_cert_path(agent).is_file() and agent_key_path(agent).is_file() and not force:
        return agent_cert_path(agent)
    # Cert presente ma SENZA identity.key lato server = identità a chiave esterna
    # (principal umano: la privkey è nel browser). Rigenerare qui sovrascriverebbe
    # il cert con un keypair del server, invalidando la recovery key. Non farlo.
    if agent_cert_path(agent).is_file() and not agent_key_path(agent).is_file() and not force:
        LOG.info("Identità '%s' a chiave esterna (cert senza key lato server): non rigenero", agent)
        return agent_cert_path(agent)
    ca_key, ca_cert = _load_ca()
    key = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, agent),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, COLONY_ORG),
            ]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=CERT_DAYS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(ca_key, algorithm=None))
    _write_private(agent_key_path(agent), key)
    CERTS_DIR.mkdir(parents=True, exist_ok=True)
    agent_cert_path(agent).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    # se era revocato, una nuova emissione lo riabilita
    revoked = _load_revoked()
    if agent in revoked:
        revoked.discard(agent)
        _save_revoked(revoked)
    LOG.info("Identità emessa per agent '%s'", agent)
    return agent_cert_path(agent)


def issue_cert_for_pubkey(name: str, pubkey_pem: str, force: bool = False) -> Path:
    """Firma un certificato CA per una PUBKEY ed25519 generata ESTERNAMENTE (es.
    dal browser, derivata dalla masterkey). Il server NON vede mai la privkey:
    riceve solo la pubkey ed emette il cert. Usato per i principal UMANI (admin).
    Scrive SOLO il cert (nessun identity.key lato server)."""
    if agent_cert_path(name).is_file() and not force:
        raise FileExistsError(f"principal '{name}' ha già un certificato")
    ca_key, ca_cert = _load_ca()
    pub = serialization.load_pem_public_key(pubkey_pem.encode())
    if not isinstance(pub, Ed25519PublicKey):
        raise ValueError("pubkey non ed25519")
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, name),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, COLONY_ORG),
            ]))
            .issuer_name(ca_cert.subject)
            .public_key(pub)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=CERT_DAYS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(ca_key, algorithm=None))
    CERTS_DIR.mkdir(parents=True, exist_ok=True)
    agent_cert_path(name).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    revoked = _load_revoked()
    if name in revoked:
        revoked.discard(name)
        _save_revoked(revoked)
    LOG.info("Cert emesso per principal esterno '%s'", name)
    return agent_cert_path(name)


def _load_revoked() -> set[str]:
    if not REVOKED_FILE.is_file():
        return set()
    try:
        return set(json.loads(REVOKED_FILE.read_text()).get("revoked", []))
    except Exception:
        return set()


def _save_revoked(revoked: set[str]) -> None:
    REVOKED_FILE.parent.mkdir(parents=True, exist_ok=True)
    REVOKED_FILE.write_text(json.dumps({"revoked": sorted(revoked)}, indent=2))


def revoke(agent: str) -> None:
    revoked = _load_revoked()
    revoked.add(agent)
    _save_revoked(revoked)
    LOG.warning("Identità REVOCATA per agent '%s'", agent)


def is_revoked(agent: str) -> bool:
    return agent in _load_revoked()


def _verify_cert(agent: str) -> Ed25519PublicKey:
    """Carica il cert dell'agente e ne verifica firma CA, validità, revoca."""
    cert_path = agent_cert_path(agent)
    if not cert_path.is_file():
        raise PermissionError(f"nessun certificato per agent '{agent}'")
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    if cn != agent:
        raise PermissionError(f"certificato CN '{cn}' ≠ agent '{agent}'")
    _ca_key, ca_cert = _load_ca()
    ca_pub = ca_cert.public_key()
    assert isinstance(ca_pub, Ed25519PublicKey)
    ca_pub.verify(cert.signature, cert.tbs_certificate_bytes)  # raises se non firma CA
    now = datetime.now(timezone.utc)
    if not (cert.not_valid_before_utc <= now <= cert.not_valid_after_utc):
        raise PermissionError(f"certificato di '{agent}' scaduto o non ancora valido")
    if is_revoked(agent):
        raise PermissionError(f"certificato di '{agent}' REVOCATO")
    pub = cert.public_key()
    assert isinstance(pub, Ed25519PublicKey)
    return pub


# ── Token di sessione ────────────────────────────────────────────────


def mint_session_token(agent: str, execution_id: str = "",
                       ttl_seconds: int = SESSION_TTL_SECONDS,
                       principal: str | None = None,
                       clearance: str | None = None,
                       on_behalf: bool = False,
                       human_role: str | None = None) -> str:
    """Firmato dal RUNNER con la chiave privata dell'agente (mai esposta
    al workspace). Nel workspace entra solo il token risultante.

    `principal` (opz.): l'utente UMANO della sessione per conto del quale l'agent
    opera — propagato al gateway così `runtime.current_user` sa con chi l'agent
    sta parlando. Verificato a monte dal runner (token umano della webui).

    `clearance` (opz.): la clearance dell'agent (SEAL-N) — propagata al gateway
    così può far rispettare clearance≥tier sull'accesso ai topic (difesa in
    profondità, asse livello). Firmata → non falsificabile dall'agent."""
    key_path = agent_key_path(agent)
    if not key_path.is_file():
        raise PermissionError(f"agent '{agent}' senza identità (eseguire pki issue)")
    key = _load_private(key_path)
    now = int(time.time())
    payload = {
        "agent": agent, "execution_id": execution_id,
        "iat": now, "exp": now + ttl_seconds, "aud": TOKEN_AUDIENCE,
    }
    if principal:
        payload["principal"] = principal
    if clearance:
        payload["clearance"] = clearance
    # M-authz: chiamata ON-BEHALF di un umano → il gateway autorizza sul ruolo
    # umano (PDP unico), non sul carrier-agent. Claim firmati → non forgiabili.
    if on_behalf:
        payload["on_behalf"] = True
        payload["human_role"] = human_role or "user"
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(key.sign(body.encode()))
    return f"{TOKEN_PREFIX}.{body}.{sig}"


CAP_PREFIX = "ccap1"


def mint_capability(agent: str, instance: str, minutes: int, by: str,
                    cap: str = "sudo") -> dict:
    """Conia un capability-token SUDO firmato dalla **CA** (non dall'agente): è
    la prova crittografica dell'approvazione umana `by`. Lo detiene/verifica il
    gateway con la CA pubblica. Firmato dalla CA → un agente NON può
    auto-emetterselo. Ritorna {token, jti, exp}.

    `by` = principal umano approvatore (dentro il payload firmato → auditabile e
    non falsificabile). `instance` = id-istanza (o "-" finché non plumbato)."""
    import secrets
    ca_key, _ = _load_ca()
    now = int(time.time())
    minutes = max(1, min(int(minutes or 15), 120))  # cap 2h
    jti = secrets.token_hex(8)
    payload = {
        "cap": cap, "agent": agent, "instance": instance or "-",
        "jti": jti, "iat": now, "exp": now + minutes * 60, "by": by,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(ca_key.sign(body.encode()))
    return {"token": f"{CAP_PREFIX}.{body}.{sig}", "jti": jti, "exp": payload["exp"]}


def verify_session_token(token: str) -> dict:
    """Valida il token e ritorna il payload. Solleva PermissionError."""
    try:
        prefix, body, sig = token.strip().split(".")
        if prefix != TOKEN_PREFIX:
            raise ValueError("prefisso token sconosciuto")
        payload = json.loads(_b64d(body))
        agent = str(payload.get("agent") or "")
        if not agent:
            raise ValueError("token senza agent")
    except PermissionError:
        raise
    except Exception as e:
        raise PermissionError(f"token malformato: {e}")
    pub = _verify_cert(agent)
    try:
        pub.verify(_b64d(sig), body.encode())
    except Exception:
        raise PermissionError(f"firma token non valida per '{agent}'")
    if payload.get("aud") != TOKEN_AUDIENCE:
        raise PermissionError("audience token errata")
    if int(payload.get("exp", 0)) < time.time():
        raise PermissionError("token scaduto")
    return payload


def verify_token_against(token: str, agent: str) -> dict:
    """Verifica la FIRMA del token contro il cert di `agent` (ignora il campo
    `agent` nel payload). Usato dal login umano per identificare il principal
    provando i cert: solo chi possiede la privkey produce una firma valida.
    Ritorna il payload; solleva PermissionError se non combacia/scaduto."""
    try:
        prefix, body, sig = token.strip().split(".")
        if prefix != TOKEN_PREFIX:
            raise ValueError("prefisso token sconosciuto")
        payload = json.loads(_b64d(body))
    except PermissionError:
        raise
    except Exception as e:
        raise PermissionError(f"token malformato: {e}")
    pub = _verify_cert(agent)
    try:
        pub.verify(_b64d(sig), body.encode())
    except Exception:
        raise PermissionError(f"firma non valida per '{agent}'")
    if payload.get("aud") != TOKEN_AUDIENCE:
        raise PermissionError("audience token errata")
    if int(payload.get("exp", 0)) < time.time():
        raise PermissionError("token scaduto")
    return payload


def has_identity(agent: str) -> bool:
    return agent_key_path(agent).is_file() and agent_cert_path(agent).is_file()


# ── CLI ──────────────────────────────────────────────────────────────


def _cli() -> None:  # pragma: no cover
    import argparse
    parser = argparse.ArgumentParser(description="PKI della colonia Clodia")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-ca")
    p_issue = sub.add_parser("issue")
    p_issue.add_argument("agent")
    p_issue.add_argument("--force", action="store_true")
    sub.add_parser("issue-all")
    p_rev = sub.add_parser("revoke")
    p_rev.add_argument("agent")
    sub.add_parser("status")
    args = parser.parse_args()

    if args.cmd == "init-ca":
        print(f"CA: {init_ca()}")
    elif args.cmd == "issue":
        print(f"cert: {issue_agent_identity(args.agent, force=args.force)}")
    elif args.cmd == "issue-all":
        from ..agents.loader import registry
        for spec in registry.list():
            # Gli UMANI generano il keypair nel browser (la recovery key è la
            # loro privkey); il server riceve solo la pubkey e firma il cert via
            # issue_cert_for_pubkey all'onboarding. NON dobbiamo mai generare un
            # keypair per loro: lo faremmo qui perché manca l'identity.key lato
            # server, sovrascrivendo il cert e invalidando la recovery key ad
            # ogni boot. Quindi salta i principal human.
            if getattr(spec, "type", None) == "human":
                print(f"skip (human): {spec.name}")
                continue
            print(f"cert: {issue_agent_identity(spec.name)}")
    elif args.cmd == "revoke":
        revoke(args.agent)
        print(f"revocato: {args.agent}")
    elif args.cmd == "status":
        print(f"CA inizializzata: {ca_initialized()}")
        if CERTS_DIR.is_dir():
            for crt in sorted(CERTS_DIR.glob("*.crt")):
                agent = crt.stem
                print(f"  {agent}: revoked={is_revoked(agent)}")


if __name__ == "__main__":  # pragma: no cover
    _cli()
