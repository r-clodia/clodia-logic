"""API dei Chat Hook.

CRUD riservato all'owner della chat (o admin di piattaforma), verificato dal
session token (principal firmato dalla CA). Ingress PUBBLICO `POST /hooks/{id}`
autorizzato dal SOLO segreto dell'hook (bearer): niente sessione.

Il percorso pubblico resta NON FIDATO. L'invocazione locale è separata, non usa
segreti ed è autorizzata solo per participant (più l'eccezione messaggero).
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Request

from . import db
from ..api import admin, topics_client
from ..api.agents import _principal_from_request

router = APIRouter()

# Rate-limit in-memory: sliding window 60s, soglia PER-HOOK (rate_per_min).
_RL_WINDOW_S = 60.0
_rl: dict[str, list[float]] = {}


def _rate_ok(hid: str, per_min: int) -> bool:
    now = time.monotonic()
    hits = [t for t in _rl.get(hid, []) if now - t < _RL_WINDOW_S]
    if len(hits) >= max(1, int(per_min)):
        _rl[hid] = hits
        return False
    hits.append(now)
    _rl[hid] = hits
    return True


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
    principal = _require_chat_owner(request, tier, name)
    topic = topics_client.open_topic(tier, name) or {}
    if topic.get("meta", {}).get("hook_enabled", True):
        try:
            db.ensure(tier, name, name, created_by=principal)
        except db.HookConflictError as e:
            raise HTTPException(409, str(e)) from e
    return {"hooks": db.list_for_chat(tier, name)}


@router.post("/clodia/chats/{tier}/{name}/hooks")
async def create_hook(tier: str, name: str, request: Request) -> dict:
    principal = _require_chat_owner(request, tier, name)
    body = await request.json()
    label = (body.get("label") or "hook").strip()
    if not label:
        raise HTTPException(400, "label richiesta")
    author = (body.get("author") or "").strip() or None
    try:
        rpm = int(body.get("rate_per_min") or 30)
    except (TypeError, ValueError):
        rpm = 30
    try:
        pub, secret = db.create(
            tier, name, label, created_by=principal, author=author, rate_per_min=rpm)
    except db.HookConflictError as e:
        raise HTTPException(409, str(e)) from e
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
    # Conserva la tombstone: un hook automatico disattivato non deve ricrearsi
    # alla successiva apertura del pannello o invocazione locale.
    return {"deleted": db.revoke(hid)}


def _payload(raw: bytes) -> str:
    payload = raw.decode("utf-8", "replace").strip()
    if payload[:1] in ("{", "["):
        try:
            payload = json.dumps(
                json.loads(payload), ensure_ascii=False, separators=(",", ":"))
        except Exception:  # noqa: BLE001
            pass
    return payload.replace("\r", " ")


def _queue_turn(tier: str, name: str, text: str, principal: str,
                responder: str | None = None) -> bool:
    try:
        from ..api.channels import run_topic_turn, _spawn_bg, _safe_name
        topic = topics_client.open_topic(tier, name)
        meta = (topic or {}).get("meta", {})
        # PROVENIENZA (issue #221). Un webhook è un sistema terzo PER
        # COSTRUZIONE: il testo è il payload che è arrivato da fuori, non la
        # richiesta di una persona. Quindi `external` è scritto qui, costante, e
        # non dedotto dal `principal` — che è un nome di configurazione: se un
        # hook venisse creato col nome di un umano registrato, dedurlo
        # rifarebbe entrare il payload come `human`.
        _spawn_bg(run_topic_turn(
            tier, name, meta, trigger_text=text, principal_hint=principal,
            responder_hint=responder, trigger_author=_safe_name(principal),
            trigger_kind="external"))
        return True
    except Exception:  # noqa: BLE001 — il messaggio resta comunque iniettato
        return False


@router.post("/clodia/hooks/internal/ensure")
async def ensure_hook(request: Request) -> dict:
    """Endpoint service-to-service usato dal gateway dopo topic.new."""
    body = await request.json()
    tier = (body.get("tier") or "").strip()
    name = (body.get("name") or "").strip()
    by = (body.get("by") or "platform").strip()
    if not tier or not name:
        raise HTTPException(400, "tier e name richiesti")
    topic = topics_client.open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "topic non trovato")
    try:
        hook, _ = db.ensure(tier, name, name, created_by=by)
    except db.HookConflictError as e:
        raise HTTPException(409, str(e)) from e
    return {"hook": hook}


@router.post("/clodia/hooks/{tier}/{name}/invoke/internal")
async def invoke_local(tier: str, name: str, request: Request) -> dict:
    """Invocazione locale: l'identità del chiamante viene dal session token
    firmato (verificato dalla CA via `_principal_from_request`), MAI da un campo
    `caller` del body — che sarebbe auto-dichiarato e impersonabile su un listener
    LAN-exposed. Nessun segreto, participant-check + eccezione messaggero."""
    caller = _principal_from_request(request)
    if not caller:
        raise HTTPException(401, "identità non autenticata (session token richiesto)")
    body = await request.json()
    payload = str(body.get("payload") or "").strip()
    if not payload:
        raise HTTPException(400, "payload richiesto")
    topic = topics_client.open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "topic non trovato")
    meta = topic.get("meta", {})
    if caller != "messaggero" and (
        caller != meta.get("owner") and caller not in (meta.get("participants") or [])
    ):
        raise HTTPException(403, f"'{caller}' non è participant di {tier}/{name}")
    row = db.get(name)
    if row and (row.get("tier"), row.get("name")) != (tier, name):
        raise HTTPException(409, f"slug '{name}' associato a un altro topic")
    if row is None:
        if not meta.get("hook_enabled", True):
            raise HTTPException(409, "hook disattivato")
        try:
            row, _ = db.ensure(tier, name, name, created_by=caller)
        except db.HookConflictError as e:
            raise HTTPException(409, str(e)) from e
    if not row.get("enabled"):
        raise HTTPException(409, "hook disabilitato")
    text = f"@{caller} {payload}"
    try:
        topics_client.post_message(tier, name, author=caller, text=text, kind="ai")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"post_message fallita: {e}") from e
    triggered = _queue_turn(tier, name, text, caller, responder=caller)
    db.record_event(name, "ok", source="local", authority="participant", principal=caller)
    return {"ok": True, "injected": True, "triggered": triggered,
            "authority": "participant", "principal": caller}


# ─── Ingress PUBBLICO (autorizzato dal segreto dell'hook) ────────────────────
@router.post("/hooks/{hid}")
async def ingress(hid: str, request: Request) -> dict:
    provided = request.headers.get("X-Hook-Secret", "") or request.query_params.get("secret", "")
    row = db.verify_secret(hid, provided)
    if not row:
        # non confermare l'esistenza: stessa risposta per id ignoto/segreto errato/disabilitato
        raise HTTPException(401, "unauthorized")
    src = request.client.host if request.client else None
    if not _rate_ok(hid, row.get("rate_per_min", 30)):
        db.record_event(hid, "rate_limited", source=src)
        raise HTTPException(429, "too many requests")

    text = _payload(await request.body())
    tier, name = row["tier"], row["name"]

    try:
        topics_client.post_message(
            tier, name, author=row["author"], text=text, kind="external")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"post_message fallita: {e}") from e

    triggered = _queue_turn(tier, name, text, "hook")
    db.record_event(hid, "ok", source=src, authority="untrusted")
    return {"ok": True, "injected": True, "triggered": triggered,
            "authority": "untrusted", "principal": None}
