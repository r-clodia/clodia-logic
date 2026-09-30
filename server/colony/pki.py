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
    python3 -m server.colony.pki revoke <agent> [--by <person>]
    python3 -m server.colony.pki flush-audit
    python3 -m server.colony.pki status
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import logging
import os
import tempfile
import threading
import time
import uuid
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
    """Genera keypair + certificato firmato dalla CA per l'agente.

    Runtime-keyless (M3++): se `CLODIA_ORCHESTRATOR_SECRET` è impostato, l'emissione
    è delegata al **gateway** (unico detentore della CA e delle identity key);
    fallback locale su errore finché le chiavi sono ancora montate."""
    if agent_cert_path(agent).is_file() and agent_key_path(agent).is_file() and not force:
        return agent_cert_path(agent)
    if (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip():
        try:
            import httpx
            secret = (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip()
            r = httpx.post(_gateway_mint_url(),
                           json={"kind": "issue-identity", "agent": agent, "force": bool(force)},
                           headers={"X-Orchestrator-Secret": secret}, timeout=10.0)
            r.raise_for_status()
            return agent_cert_path(agent)
        except Exception as e:  # noqa: BLE001
            LOG.warning("issue-identity via gateway fallito per %s (%s) → locale", agent, e)
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
    # If it was revoked, a new issuance re-enables it — and that is recorded (#466).
    _clear_revocation(agent, via="issue_agent_identity")
    LOG.info("Identità emessa per agent '%s'", agent)
    return agent_cert_path(agent)


def issue_cert_for_pubkey(name: str, pubkey_pem: str, force: bool = False) -> Path:
    """Firma un certificato CA per una PUBKEY ed25519 generata ESTERNAMENTE (es.
    dal browser, derivata dalla masterkey). Il server NON vede mai la privkey:
    riceve solo la pubkey ed emette il cert. Usato per i principal UMANI (admin).
    Scrive SOLO il cert (nessun identity.key lato server).

    Se `CLODIA_ORCHESTRATOR_SECRET` è impostato, l'emissione è delegata al
    **gateway** (unico detentore della CA key); fallback locale su errore."""
    if (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip():
        try:
            import httpx
            secret = (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip()
            r = httpx.post(_gateway_mint_url(),
                           json={"kind": "issue-cert", "agent": name,
                                 "pubkey_pem": pubkey_pem, "force": bool(force)},
                           headers={"X-Orchestrator-Secret": secret}, timeout=10.0)
            r.raise_for_status()
            return agent_cert_path(name)
        except Exception as e:  # noqa: BLE001
            LOG.warning("issue-cert via gateway fallito per %s (%s) → locale", name, e)
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
    _clear_revocation(name, via="issue_cert_for_pubkey")
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


# ── Audit of revocation (clodia-platform#466) ────────────────────────────────
# `revoked.json` is a rule of the reference monitor: a principal in it is
# refused everywhere. Changing it is a control-plane change like any other, and
# it happens here, through the CLI, where no HTTP middleware can see it. So the
# two functions that change the file — `revoke` and `_clear_revocation` — each
# record a `control.pki` event on the gateway's trail, through the same
# agent-server → gateway channel the turns use (`audit_events`).
#
# Order: the revocation takes effect FIRST, then it is recorded. A compromised
# identity must not stay valid because the trail is unreachable. If the gateway
# does not confirm the record, the event goes to a local outbox (delivered, in
# order, before the next PKI event and at every server boot) and the change is
# reported loudly: `revoke` raises, the CLI exits non-zero. Never silent.
#
# Consequence, intended: without CLODIA_ORCHESTRATOR_SECRET (a deployment with
# no gateway trail, i.e. not M3++) there is nowhere to record, so EVERY CLI
# revoke exits 2 and the outbox grows until a gateway is paired.

AUDIT_OUTBOX_NAME = "audit-outbox.jsonl"

#: The person behind a CLI invocation (`--by`), set by `_cli`. None in the
#: server, where a PKI change without an explicit actor is the service's own.
_CLI_ACTOR: Optional[dict] = None


class RevocationNotRecorded(RuntimeError):
    """The revocation change is IN EFFECT but the trail did not confirm it: the
    event waits in the outbox. Raised so that the caller cannot miss it."""


def _outbox_path() -> Path:
    return PKI_DIR / AUDIT_OUTBOX_NAME


def _cert_serial(name: str) -> Optional[str]:
    """Serial of the certificate currently on file for `name`, as hex (the
    gateway's `control.pki` issue events use the same form)."""
    try:
        cert = x509.load_pem_x509_certificate(agent_cert_path(name).read_bytes())
        return format(cert.serial_number, "x")
    except Exception:  # noqa: BLE001 - no cert, or unreadable: no serial
        return None


def _default_actor() -> dict:
    if _CLI_ACTOR:
        return dict(_CLI_ACTOR)
    return {"type": "service", "id": "agent-server"}


def _revocation_event(action: str, principal: str, *, actor: Optional[dict],
                      via: str) -> dict:
    # `event_id`: stable across re-sends (the outbox re-sends an event whose
    # confirmation was lost), so the gateway can deduplicate; also in `result`,
    # so it is on the trail for an auditor.
    eid = uuid.uuid4().hex
    return {"type": "control.pki", "action": action, "resource": principal,
            "event_id": eid,
            "actor": dict(actor) if actor else _default_actor(),
            "result": {"principal": principal, "cert_serial": _cert_serial(principal),
                       "via": via, "source_event_id": eid,
                       "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}}


@contextlib.contextmanager
def _outbox_lock():
    """Exclusive lock on the outbox across PROCESSES: the server flushes it (at
    boot, on an unrevoke) while a CLI may be queueing into it. Without it a
    flush that rewrites the file could drop an event the CLI has just reported
    as queued. `flock` on a separate lock file, so the outbox itself can be
    replaced atomically underneath."""
    path = _outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path.with_name(AUDIT_OUTBOX_NAME + ".lock")),
                 os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _outbox_append(event: dict) -> None:
    line = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
    with _outbox_lock():
        # 0600 from the first byte: the file is created with its mode.
        fd = os.open(str(_outbox_path()), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)


def _read_outbox() -> list[dict]:
    path = _outbox_path()
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            LOG.error("pki audit outbox: unreadable line kept out of the trail")
    return out


def pending_audit_events() -> list[dict]:
    """The PKI events still waiting for the trail, oldest first."""
    with _outbox_lock():
        return _read_outbox()


def flush_audit_outbox() -> int:
    """Deliver the queued PKI events, in order; stop at the first one the
    gateway does not confirm (a later event must not overtake an earlier one).
    Returns how many were delivered.

    The whole flush holds the outbox lock: two flushers cannot send the same
    event twice, and an event queued meanwhile waits for the lock instead of
    being overwritten by the rewrite. If the gateway records an event but its
    answer is lost, the event is sent again: it carries a stable `event_id`
    (minted when the event was built) for the gateway to deduplicate on."""
    from .. import audit_events
    with _outbox_lock():
        pending = _read_outbox()
        if not pending:
            return 0
        sent = 0
        for ev in pending:
            late = {**ev, "result": {**(ev.get("result") or {}), "recorded_late": True}}
            if not audit_events.report_sync(late):
                break
            sent += 1
        rest = pending[sent:]
        path = _outbox_path()
        if rest:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".audit-outbox.",
                                       suffix=".tmp")   # mkstemp: 0600, unique name
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write("".join(json.dumps(e, sort_keys=True) + "\n" for e in rest))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        else:
            path.unlink(missing_ok=True)
    if sent:
        LOG.warning("pki audit outbox: %d queued event(s) delivered to the trail", sent)
    return sent


def _record_revocation(action: str, principal: str, *, actor: Optional[dict],
                       via: str, strict: bool) -> bool:
    """Record a change of `revoked.json` on the trail. True if recorded.

    Not recorded → queued in the outbox, and then either raised (`strict`) or
    logged as an ERROR: the change itself is never undone."""
    event = _revocation_event(action, principal, actor=actor, via=via)
    try:
        flush_audit_outbox()
    except Exception as e:  # noqa: BLE001 - a stuck outbox must not hide THIS event
        LOG.error("pki audit outbox not flushed (%s)", type(e).__name__)
    from .. import audit_events
    if not pending_audit_events() and audit_events.report_sync(event):
        return True
    # Behind a queue that did not drain, this event queues too: order matters.
    _outbox_append(event)
    msg = (f"control.pki {action} of '{principal}' is IN EFFECT but NOT recorded on "
           f"the audit trail (gateway unreachable, or no CLODIA_ORCHESTRATOR_SECRET): "
           f"queued in {_outbox_path()}, delivered at the next PKI change or server boot")
    if strict:
        raise RevocationNotRecorded(msg)
    LOG.error(msg)
    return False


def _clear_revocation(name: str, *, via: str, actor: Optional[dict] = None) -> bool:
    """A new certificate re-enables a revoked principal: remove it from
    `revoked.json` and record the `unrevoke`. No-op if it was not revoked.

    Strict only from the CLI, where a person is watching: in the server the
    issuance must not fail because the trail is down — the event is queued and
    the error logged."""
    revoked = _load_revoked()
    if name not in revoked:
        return False
    revoked.discard(name)
    _save_revoked(revoked)
    LOG.warning("Revocation CLEARED for '%s' (new certificate issued)", name)
    _record_revocation("unrevoke", name, actor=actor, via=via,
                       strict=_CLI_ACTOR is not None)
    return True


def revoke(agent: str, *, actor: Optional[dict] = None, via: str = "revoke") -> bool:
    """Revoke `agent`'s identity and record it on the trail (`control.pki`
    `revoke`: principal, actor, serial of the revoked certificate).

    Returns False if it was already revoked (nothing changed, nothing to
    record). Raises `RevocationNotRecorded` if the revocation took effect but
    the trail did not confirm it — always so without CLODIA_ORCHESTRATOR_SECRET,
    where there is no trail to record on: the event waits in the outbox."""
    revoked = _load_revoked()
    if agent in revoked:
        LOG.info("Identity of '%s' already revoked: nothing changed", agent)
        return False
    revoked.add(agent)
    _save_revoked(revoked)
    LOG.warning("Identity REVOKED for agent '%s'", agent)
    _record_revocation("revoke", agent, actor=actor, via=via, strict=True)
    return True


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
    # Verifica: serve SOLO la CA pubblica (ca.crt), non la privata. In modalità
    # runtime-keyless (M3++) agent-server monta la sola ca.crt (bind ro), non
    # ca.key → NON usare _load_ca() (pretende la privata). Basta il cert pubblico.
    if not CA_CRT.is_file():
        raise PermissionError("CA cert (ca.crt) assente: impossibile verificare la catena")
    ca_cert = x509.load_pem_x509_certificate(CA_CRT.read_bytes())
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


# ── Delega del minting al gateway (trust-anchor) ─────────────────────────────
# Target M3++: le chiavi private NON vivono nel container dell'orchestrator. Se
# `CLODIA_ORCHESTRATOR_SECRET` è impostato, il token si chiede al gateway
# (`/internal/mint`) invece di firmarlo qui. Cache per-tupla-identità (TTL) per
# evitare un round-trip HTTP a ogni chiamata (topics_client conia per-request).
# Flag SPENTO = comportamento storico (firma locale): rollout sicuro/reversibile.
_MINT_CACHE: dict[tuple, tuple[str, int]] = {}
_MINT_CACHE_SKEW = 120  # ri-conia 2 min prima della scadenza

# Il ciclo «leggi scadenza → conia → scrivi in cache» era atomico PER COSTRUZIONE
# finché i client (topics_client/provider_store/gateway_admin) giravano tutti sul
# thread dell'event loop: nessun await in mezzo, nessun interleaving. Con
# l'offload su `asyncio.to_thread` (#106) quel ciclo può girare in PARALLELO su
# più thread: due richieste che trovano la cache scaduta coniano entrambe e la
# seconda scrittura sovrascrive la prima. Non è una gara che c'era prima: la
# introduce l'offload, quindi il lock nasce nello stesso commit.
#
# Un lock PER-CHIAVE, non globale: la sezione critica contiene una POST HTTP
# (timeout 8s) e un lock unico farebbe convoglio fra identità diverse,
# serializzando mint che non condividono nulla. Il lock del dict copre solo
# l'accesso alle strutture, mai la rete.
_MINT_STATE_LOCK = threading.Lock()
_MINT_KEY_LOCKS: dict[tuple, threading.Lock] = {}
_MINT_LOCKS_MAX = 512  # oltre questa soglia si potano le voci scadute


def _mint_key_lock(key: tuple) -> threading.Lock:
    """Lock dedicato alla chiave di cache (creato una volta sola)."""
    with _MINT_STATE_LOCK:
        lock = _MINT_KEY_LOCKS.get(key)
        if lock is None:
            if len(_MINT_KEY_LOCKS) >= _MINT_LOCKS_MAX:
                _prune_mint_locks_locked()
            lock = _MINT_KEY_LOCKS[key] = threading.Lock()
        return lock


def _prune_mint_locks_locked() -> None:
    """Scarta i lock delle chiavi il cui token è scaduto/assente (già dentro
    `_MINT_STATE_LOCK`). Le chiavi contengono l'execution_id: senza potatura il
    dizionario crescerebbe per tutta la vita del processo."""
    now = int(time.time())
    for k, lk in list(_MINT_KEY_LOCKS.items()):
        hit = _MINT_CACHE.get(k)
        if hit is not None and hit[1] > now:
            continue  # token ancora valido: il lock serve
        if lk.locked():
            continue  # mint in corso su quella chiave: non si tocca
        _MINT_KEY_LOCKS.pop(k, None)
        _MINT_CACHE.pop(k, None)


def _gateway_mint_url() -> str:
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/")
    base = mcp.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return base + "/internal/mint"


def _mint_via_gateway(agent: str, execution_id: str, ttl_seconds: int,
                      principal: Optional[str], clearance: Optional[str],
                      on_behalf: bool, human_role: Optional[str],
                      chat: Optional[str],
                      scoped_tools: Optional[list[str]],
                      unattended: bool = False,
                      scope_tier: Optional[str] = None,
                      origin: Optional[list[str]] = None) -> str:
    scoped_key = tuple(scoped_tools or ())
    origin_key = tuple(str(x) for x in (origin or ()))
    # `unattended` ENTRA nella chiave di cache: senza, un token coniato per una
    # chat umana verrebbe riusato per un job dello stesso agente e il blocco
    # salterebbe silenziosamente.
    key = (agent, execution_id, int(ttl_seconds), principal, clearance, on_behalf,
           scope_tier, origin_key,
           human_role, chat, scoped_key, bool(unattended))
    hit = _cached_mint(key)
    if hit is not None:
        return hit
    # SEZIONE UNICA: rilettura della scadenza, mint e scrittura in cache stanno
    # dentro lo stesso lock. Proteggere la sola scrittura lascerebbe in piedi il
    # doppio mint, che è esattamente il difetto da chiudere.
    with _mint_key_lock(key):
        # Doppio controllo: chi ha atteso il lock trova il token coniato da chi
        # l'ha preceduto e non ne conia un secondo.
        hit = _cached_mint(key)
        if hit is not None:
            return hit
        now = int(time.time())
        token = _mint_request(agent, execution_id, ttl_seconds, principal,
                              clearance, on_behalf, human_role, chat,
                              scoped_key, unattended, scope_tier=scope_tier,
                              origin=list(origin_key) or None)
        with _MINT_STATE_LOCK:
            _MINT_CACHE[key] = (token, now + int(ttl_seconds))
        return token


def _cached_mint(key: tuple) -> str | None:
    """Token in cache ancora valido (con skew), o None."""
    with _MINT_STATE_LOCK:
        hit = _MINT_CACHE.get(key)
    if hit and hit[1] - _MINT_CACHE_SKEW > int(time.time()):
        return hit[0]
    return None


def _mint_request(agent: str, execution_id: str, ttl_seconds: int,
                  principal: Optional[str], clearance: Optional[str],
                  on_behalf: bool, human_role: Optional[str],
                  chat: Optional[str], scoped_key: tuple,
                  unattended: bool, scope_tier: Optional[str] = None,
                  origin: Optional[list[str]] = None) -> str:
    """La sola chiamata al gateway (nessuna cache): la tiene fuori dal lock del
    dizionario, dentro il lock della chiave."""
    import httpx
    secret = (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip()
    body = {"kind": "session", "agent": agent, "execution_id": execution_id,
            "ttl_seconds": int(ttl_seconds), "principal": principal,
            "clearance": clearance, "on_behalf": bool(on_behalf),
            "human_role": human_role, "chat": chat,
            "scoped_tools": list(scoped_key), "unattended": bool(unattended),
            "scope_tier": scope_tier, "origin": list(origin) if origin else None}
    r = httpx.post(_gateway_mint_url(), json=body,
                   headers={"X-Orchestrator-Secret": secret}, timeout=8.0)
    if r.status_code == 403 and "senza identità" in (r.text or ""):
        # PRIMO BOOT di un'istanza keyless: l'entrypoint dichiara "PKI bootstrap
        # delegata al gateway", il gateway sa emettere le identità ma NESSUNO
        # gliele chiede. In mezzo non c'è nessuno, e l'istanza è inutilizzabile:
        # senza identity.key ogni mint dà 403 e `/api/agents` muore in 500.
        #
        # Si chiede l'emissione una volta e si ritenta. Idempotente: il gateway
        # non rigenera un'identità esistente.
        #
        # SOLO per agenti, mai per umani: un principal umano ha la chiave nel
        # BROWSER e il server non deve possederne una: emettergliela qui
        # significherebbe poter firmare al suo posto. La distinzione la conosce
        # solo il registry, che vive qui — è la ragione per cui l'auto-emissione
        # sta da questo lato e non nel gateway, che dal suo config.yaml non sa
        # chi è umano.
        if _is_human_principal(agent):
            LOG.warning("mint: '%s' è un principal umano senza identità — NON la "
                        "emetto (la sua chiave sta nel browser)", agent)
            r.raise_for_status()
        LOG.warning("mint: '%s' senza identità nel gateway → chiedo l'emissione", agent)
        httpx.post(_gateway_mint_url(),
                   json={"kind": "issue-identity", "agent": agent},
                   headers={"X-Orchestrator-Secret": secret},
                   timeout=15.0).raise_for_status()
        r = httpx.post(_gateway_mint_url(), json=body,
                       headers={"X-Orchestrator-Secret": secret}, timeout=8.0)
    r.raise_for_status()
    return r.json()["token"]


def _is_human_principal(agent: str) -> bool:
    """True se `agent` è un principal UMANO. Fail-CLOSED verso «umano»: se non si
    riesce a stabilirlo, NON si emette una chiave — sbagliare in questa direzione
    costa un mint fallito, nell'altra costa la capacità di firmare per una persona.
    """
    try:
        from ..agents.loader import registry
        spec = registry.get_by_name(agent)
        if spec is None:
            return True
        return (getattr(spec, "type", "") or "") == "human"
    except Exception as e:  # noqa: BLE001
        LOG.warning("tipo di '%s' non determinabile (%s): assumo umano", agent, e)
        return True


def mint_session_token(agent: str, execution_id: str = "",
                       ttl_seconds: int = SESSION_TTL_SECONDS,
                       principal: str | None = None,
                       clearance: str | None = None,
                       on_behalf: bool = False,
                       human_role: str | None = None,
                       chat: str | None = None,
                       scoped_tools: list[str] | None = None,
                       unattended: bool = False,
                       origin: list[str] | None = None,
                       scope_tier: str | None = None) -> str:
    """Firmato dal RUNNER con la chiave privata dell'agente (mai esposta
    al workspace). Nel workspace entra solo il token risultante.

    Se `CLODIA_ORCHESTRATOR_SECRET` è impostato, il minting è delegato al
    **gateway** (le chiavi private stanno solo lì); su errore si ripiega sulla
    firma locale finché le chiavi sono ancora montate (rollout sicuro).

    `principal` (opz.): l'utente UMANO della sessione per conto del quale l'agent
    opera — propagato al gateway così `runtime.current_user` sa con chi l'agent
    sta parlando. Verificato a monte dal runner (token umano della webui).

    `clearance` (opz.): la clearance dell'agent (SEAL-N) — propagata al gateway
    così può far rispettare clearance≥tier sull'accesso ai topic (difesa in
    profondità, asse livello). Firmata → non falsificabile dall'agent."""
    scoped_tools = list(dict.fromkeys(scoped_tools or []))
    if any(t in ("*", "agents") or t.startswith("agents.") for t in scoped_tools):
        raise PermissionError("scoped_tools non può concedere wildcard o tool agents.*")
    if (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip():
        try:
            # EVERY claim of the local signer below goes to the gateway too
            # (clodia-platform#450): `origin` and `scope_tier` were dropped here,
            # so in production the signed token carried neither.
            return _mint_via_gateway(agent, execution_id, ttl_seconds, principal,
                                     clearance, on_behalf, human_role, chat,
                                     scoped_tools, unattended, scope_tier=scope_tier,
                                     origin=origin)
        except Exception as e:  # noqa: BLE001
            LOG.warning("mint via gateway fallito per %s (%s) → firma locale", agent, e)
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
    if chat:
        payload["chat"] = chat  # chat_id della sessione (per postare in chat le decisioni sudo)
    if scope_tier:
        # TIER DELLO SCOPE in cui gira questa esecuzione, quando non è una stanza.
        # Un job È uno scope e dichiara un tier (voce 33), ma quel tier non
        # arrivava al gateway: `current_channel()` è None per un job, quindi la
        # regola della portabilità — «un topic portato viaggia solo dove la
        # stanza lo regge» — era scritta e non applicata proprio lì.
        payload["scope_tier"] = scope_tier
    if scoped_tools:
        payload["scoped_tools"] = scoped_tools
    if origin:
        # CATENA D'ORIGINE (docs/specification.md §3.3): chi ha causato questo
        # turno, dall'iniziatore all'esecutore. Firmata come `chat` e
        # `clearance`, e per lo stesso motivo — il gateway interseca le autorità
        # della catena, e se un agente potesse comporla l'intersezione sarebbe la
        # sua parola su sé stesso.
        payload["origin"] = [str(x) for x in origin]
    if unattended:
        # Sessione aperta da un job schedulato: nessun umano davanti al turno.
        # Claim FIRMATO → l'agente non può togliersela (clodia-platform#104).
        payload["unattended"] = True
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
    non falsificabile). `instance` = id-istanza (o "-" finché non plumbato).

    Runtime-keyless (M3++): se `CLODIA_ORCHESTRATOR_SECRET` è impostato, la firma
    (che richiede la CA) è delegata al **gateway** (kind 'capability'); fallback
    locale su errore finché la CA è ancora montata."""
    if (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip():
        try:
            import httpx
            secret = (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip()
            r = httpx.post(_gateway_mint_url(),
                           json={"kind": "capability", "agent": agent,
                                 "instance": instance, "minutes": minutes,
                                 "by": by, "cap": cap},
                           headers={"X-Orchestrator-Secret": secret}, timeout=10.0)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            LOG.warning("mint_capability via gateway fallito per %s (%s) → locale", agent, e)
    import secrets
    ca_key, _ = _load_ca()
    now = int(time.time())
    # cap 2h; 24h solo per `copybrain` (clodia-platform#393), stessa regola del
    # gateway (`pki_mint.capability_ceiling_minutes`): vale «fino a fine spawn»,
    # e la fine vera la chiude `api.gate.release_spawn_loans`.
    tetto = 24 * 60 if str(cap or "").startswith("gate:copybrain:") else 120
    minutes = max(1, min(int(minutes or 15), tetto))
    jti = secrets.token_hex(8)
    payload = {
        "cap": cap, "agent": agent, "instance": instance or "-",
        "jti": jti, "iat": now, "exp": now + minutes * 60, "by": by,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(ca_key.sign(body.encode()))
    return {"token": f"{CAP_PREFIX}.{body}.{sig}", "jti": jti, "exp": payload["exp"]}


def verify_capability(token: str) -> dict:
    """Validate a CA-signed capability token without requiring the CA private key."""
    try:
        prefix, body, sig = token.strip().split(".")
        if prefix != CAP_PREFIX:
            raise ValueError("prefisso capability sconosciuto")
        payload = json.loads(_b64d(body))
    except Exception as e:
        raise PermissionError(f"capability malformata: {e}")
    if not CA_CRT.is_file():
        raise PermissionError("CA cert non disponibile")
    pub = x509.load_pem_x509_certificate(CA_CRT.read_bytes()).public_key()
    if not isinstance(pub, Ed25519PublicKey):
        raise PermissionError("CA non Ed25519")
    try:
        pub.verify(_b64d(sig), body.encode())
    except Exception as e:
        raise PermissionError("firma capability non valida") from e
    if not str(payload.get("cap") or "").strip():
        raise PermissionError("capability senza cap")
    if int(payload.get("exp", 0)) < time.time():
        raise PermissionError("capability scaduta")
    return payload


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
    sub.add_parser("flush-audit")
    for p in (p_issue, p_rev):
        # Who is doing this, for the audit trail (#466). The OS user of a
        # container is `root` for everyone: say who you are.
        p.add_argument("--by", default=None,
                       help="who performs the change (recorded on the audit trail)")
    args = parser.parse_args()
    global _CLI_ACTOR
    import getpass
    by = (getattr(args, "by", None) or os.environ.get("CLODIA_ACTOR") or "").strip()
    try:
        os_user = getpass.getuser()
    except Exception:  # noqa: BLE001
        os_user = None
    _CLI_ACTOR = {"type": "human" if by else "operator", "id": by or os_user,
                  "via": "pki-cli", "os_user": os_user}

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
        try:
            changed = revoke(args.agent)
        except RevocationNotRecorded as e:
            print(f"revoked: {args.agent}\nERROR: {e}")
            raise SystemExit(2)
        print(f"revoked: {args.agent}" if changed else f"already revoked: {args.agent}")
    elif args.cmd == "flush-audit":
        n = flush_audit_outbox()
        left = len(pending_audit_events())
        print(f"audit events delivered: {n}, still queued: {left}")
        if left:
            raise SystemExit(2)
    elif args.cmd == "status":
        print(f"CA inizializzata: {ca_initialized()}")
        if CERTS_DIR.is_dir():
            for crt in sorted(CERTS_DIR.glob("*.crt")):
                agent = crt.stem
                print(f"  {agent}: revoked={is_revoked(agent)}")


if __name__ == "__main__":  # pragma: no cover
    _cli()
