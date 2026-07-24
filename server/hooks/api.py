"""API dei Chat Hook (F1).

CRUD riservato all'owner della chat (o admin di piattaforma), verificato dal
session token (principal firmato dalla CA). Ingress PUBBLICO `POST /hooks/{id}`
autorizzato dal SOLO segreto dell'hook (bearer): niente sessione.

F1 — percorso NON FIDATO: il messaggio iniettato entra con autore `hook:<label>`
e (se l'hook ha un trigger) sveglia il responder con `principal_hint="hook"`, che
NON eredita autorità umana → ogni azione fuori-topic resta gated (M-gate). La
firma con identità CA (autorità piena) è F2.
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Request

from . import db
from ..api import admin, topics_client
from ..api.agents import _principal_from_request

router = APIRouter()

# Rate-limit in-memory molto semplice (F1): max N richieste / finestra per hook.
_RL_WINDOW_S = 10.0
_RL_MAX = 5
_rl: dict[str, list[float]] = {}


def _rate_ok(hid: str) -> bool:
    now = time.monotonic()
    hits = [t for t in _rl.get(hid, []) if now - t < _RL_WINDOW_S]
    if len(hits) >= _RL_MAX:
        _rl[hid] = hits
        return False
    hits.append(now)
    _rl[hid] = hits
    return True


_SIG_WINDOW_S = 300  # anti-replay: la firma copre id.timestamp.body


def _verify_signature(hid: str, ts: str, sig_b64: str, identity: str, raw: bytes) -> str:
    """Verifica la firma Ed25519 della richiesta contro il cert CA dell'identità.
    Ritorna il principal verificato, o solleva 401. Firma su `f"{id}.{ts}."` + body
    grezzo. Timestamp unix (s) entro ±_SIG_WINDOW_S (anti-replay)."""
    import base64
    try:
        t = int(ts)
    except (TypeError, ValueError):
        raise HTTPException(401, "timestamp non valido")
    now = int(time.time())
    if abs(now - t) > _SIG_WINDOW_S:
        raise HTTPException(401, "timestamp fuori finestra (replay?)")
    from ..colony import pki
    try:
        pub = pki._verify_cert(identity)  # CA + validità + revoca (raises)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(401, f"identità non valida: {e}") from e
    try:
        sig = base64.b64decode(sig_b64)
        pub.verify(sig, f"{hid}.{t}.".encode("utf-8") + raw)
    except Exception:  # noqa: BLE001 — InvalidSignature o base64 malformato
        raise HTTPException(401, "firma non valida")
    return identity


def _require_chat_owner(request: Request, tier: str, name: str) -> str:
    """Il principal deve essere owner della chat o admin di piattaforma."""
    principal = _principal_from_request(request)
    if not principal:
        raise HTTPException(401, "login richiesto")
    topic = topics_client.open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "chat non trovata")
    meta = topic.get("meta", {})
    if principal != meta.get("owner") and not admin.is_admin(principal):
        raise HTTPException(403, "solo l'owner della chat (o un admin) può gestire gli hook")
    return principal


# ─── CRUD (owner/admin) ────────────────────────────────────────────────────
@router.get("/clodia/chats/{tier}/{name}/hooks")
async def list_hooks(tier: str, name: str, request: Request) -> dict:
    _require_chat_owner(request, tier, name)
    return {"hooks": db.list_for_chat(tier, name)}


@router.post("/clodia/chats/{tier}/{name}/hooks")
async def create_hook(tier: str, name: str, request: Request) -> dict:
    principal = _require_chat_owner(request, tier, name)
    body = await request.json()
    label = (body.get("label") or "hook").strip()
    if not label:
        raise HTTPException(400, "label richiesta")
    trig = (body.get("trigger_agent") or "").strip() or None
    author = (body.get("author") or "").strip() or None
    pub, secret = db.create(tier, name, label, created_by=principal,
                            author=author, trigger_agent=trig)
    base = str(request.base_url).rstrip("/")
    return {
        "hook": pub,
        "secret": secret,               # mostrato UNA sola volta
        "path": f"/hooks/{pub['id']}",
        "url": f"{base}/hooks/{pub['id']}",
    }


@router.post("/clodia/hooks/{hid}/revoke")
async def revoke_hook(hid: str, request: Request) -> dict:
    row = db.get(hid)
    if not row:
        raise HTTPException(404, "hook non trovato")
    _require_chat_owner(request, row["tier"], row["name"])
    return {"revoked": db.revoke(hid)}


@router.delete("/clodia/hooks/{hid}")
async def delete_hook(hid: str, request: Request) -> dict:
    row = db.get(hid)
    if not row:
        raise HTTPException(404, "hook non trovato")
    _require_chat_owner(request, row["tier"], row["name"])
    return {"deleted": db.delete(hid)}


# ─── Identità firmatarie (F2): emissione/revoca cert CA per mittenti esterni ──
# Un mittente esterno genera una coppia Ed25519, ci manda la SOLA pubkey (PEM) e
# noi emettiamo un cert della CA per un `name`. Firmando le richieste, il webhook
# entra con AUTORITÀ PIENA di quel principal. Solo admin (evita impersonation).
@router.post("/clodia/hook-identities")
async def enroll_identity(request: Request) -> dict:
    principal = _principal_from_request(request)
    if not admin.is_admin(principal):
        raise HTTPException(403, "solo un admin può emettere identità firmatarie")
    body = await request.json()
    name = (body.get("name") or "").strip()
    pem = (body.get("pubkey_pem") or "").strip()
    force = bool(body.get("force"))
    if not name or not pem:
        raise HTTPException(400, "name e pubkey_pem richiesti")
    from ..colony import pki
    try:
        pki.issue_cert_for_pubkey(name, pem, force=force)
    except FileExistsError:
        raise HTTPException(409, f"identità '{name}' esiste già (usa force per rigenerare)")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"emissione cert fallita: {e}") from e
    return {"ok": True, "name": name}


@router.delete("/clodia/hook-identities/{name}")
async def revoke_identity(name: str, request: Request) -> dict:
    principal = _principal_from_request(request)
    if not admin.is_admin(principal):
        raise HTTPException(403, "solo un admin può revocare identità")
    from ..colony import pki
    pki.revoke(name)
    return {"ok": True, "revoked": name}


# ─── Ingress PUBBLICO (autorizzato dal segreto dell'hook) ────────────────────
@router.post("/hooks/{hid}")
async def ingress(hid: str, request: Request) -> dict:
    provided = request.headers.get("X-Hook-Secret", "") or request.query_params.get("secret", "")
    row = db.verify_secret(hid, provided)
    if not row:
        # non confermare l'esistenza: stessa risposta per id ignoto/segreto errato/disabilitato
        raise HTTPException(401, "unauthorized")
    if not _rate_ok(hid):
        raise HTTPException(429, "too many requests")

    raw = await request.body()

    # F2 — FIRMA CA (opzionale): se la richiesta è firmata con un'identità emessa
    # dalla CA della colony, il messaggio porta QUEL principal verificato →
    # AUTORITÀ PIENA (come un utente loggato). Firma incompleta/errata → 401 (mai
    # downgrade silenzioso). Nessun header di firma → percorso NON FIDATO (F1).
    ident = request.headers.get("X-Hook-Identity", "").strip()
    sig = request.headers.get("X-Hook-Signature", "").strip()
    ts = request.headers.get("X-Hook-Timestamp", "").strip()
    signed_principal = None
    if ident or sig or ts:
        if not (ident and sig and ts):
            raise HTTPException(401, "firma incompleta: servono X-Hook-Identity, -Signature, -Timestamp")
        signed_principal = _verify_signature(hid, ts, sig, ident, raw)

    payload = raw.decode("utf-8", "replace").strip()
    if payload[:1] in ("{", "["):
        try:
            payload = json.dumps(json.loads(payload), ensure_ascii=False, separators=(",", ":"))
        except Exception:  # noqa: BLE001 — non JSON valido: lascia il testo grezzo
            pass
    payload = payload.replace("\r", " ")

    tier, name, trig = row["tier"], row["name"], row.get("trigger_agent")
    text = f"@{trig} {payload}" if trig else payload
    src = request.client.host if request.client else None

    # Autore + autorità in base alla firma.
    if signed_principal:
        author, kind, principal_hint = signed_principal, "human", signed_principal
    else:
        author, kind, principal_hint = row["author"], "external", "hook"

    try:
        topics_client.post_message(tier, name, author=author, text=text, kind=kind)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"post_message fallita: {e}") from e

    triggered = False
    if trig:
        # sveglia il responder in-process (l'ingress è già autorizzato dal segreto).
        # principal_hint: identità verificata → autorità piena; "hook" → nessuna
        # autorità umana → azioni fuori-topic gated.
        try:
            from ..api.channels import run_topic_turn, _spawn_bg
            topic = topics_client.open_topic(tier, name)
            meta = (topic or {}).get("meta", {})
            _spawn_bg(run_topic_turn(tier, name, meta, trigger_text=text, principal_hint=principal_hint))
            triggered = True
        except Exception:  # noqa: BLE001 — il messaggio è comunque iniettato
            triggered = False

    db.touch(hid, src)
    return {"ok": True, "injected": True, "triggered": triggered,
            "authority": "identity" if signed_principal else "untrusted",
            "principal": signed_principal}
