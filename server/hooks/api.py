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

    raw = (await request.body()).decode("utf-8", "replace").strip()
    payload = raw
    if payload[:1] in ("{", "["):
        try:
            payload = json.dumps(json.loads(payload), ensure_ascii=False, separators=(",", ":"))
        except Exception:  # noqa: BLE001 — non JSON valido: lascia il testo grezzo
            pass
    payload = payload.replace("\r", " ")

    tier, name, trig = row["tier"], row["name"], row.get("trigger_agent")
    text = f"@{trig} {payload}" if trig else payload
    src = request.client.host if request.client else None
    try:
        topics_client.post_message(tier, name, author=row["author"], text=text, kind="external")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"post_message fallita: {e}") from e

    triggered = False
    if trig:
        # sveglia il responder in-process (l'ingress è già autorizzato dal segreto);
        # principal_hint="hook" → nessuna autorità umana → azioni fuori-topic gated.
        try:
            from ..api.channels import run_topic_turn, _spawn_bg
            topic = topics_client.open_topic(tier, name)
            meta = (topic or {}).get("meta", {})
            _spawn_bg(run_topic_turn(tier, name, meta, trigger_text=text, principal_hint="hook"))
            triggered = True
        except Exception:  # noqa: BLE001 — il messaggio è comunque iniettato
            triggered = False

    db.touch(hid, src)
    return {"ok": True, "injected": True, "triggered": triggered}
