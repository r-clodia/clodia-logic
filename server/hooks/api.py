"""API dei Chat Hook.

CRUD riservato all'owner della chat (o admin di piattaforma), verificato dal
session token (principal firmato dalla CA). Ingress PUBBLICO `POST /hooks/{id}`
autorizzato dal SOLO segreto dell'hook (bearer): niente sessione.

Il percorso pubblico resta NON FIDATO. L'invocazione locale è separata, non usa
segreti ed è autorizzata solo per participant (più l'eccezione messaggero).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request

from . import db
from ..api import admin, topics_client
from ..api.agents import _principal_from_request

LOG = logging.getLogger("agent-server.hooks.api")

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
    principal = await asyncio.to_thread(_require_chat_owner, request, tier, name)
    topic = await topics_client.async_open_topic(tier, name) or {}
    if topic.get("meta", {}).get("hook_enabled", True):
        try:
            db.ensure(tier, name, name, created_by=principal)
        except db.HookConflictError as e:
            raise HTTPException(409, str(e)) from e
    return {"hooks": db.list_for_chat(tier, name)}


@router.post("/clodia/chats/{tier}/{name}/hooks")
async def create_hook(tier: str, name: str, request: Request) -> dict:
    principal = await asyncio.to_thread(_require_chat_owner, request, tier, name)
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
    await asyncio.to_thread(_require_chat_owner, request, row["tier"], row["name"])
    return {"revoked": db.revoke(hid)}


@router.delete("/clodia/hooks/{hid}")
async def delete_hook(hid: str, request: Request) -> dict:
    row = db.get(hid)
    if not row:
        raise HTTPException(404, "hook non trovato")
    await asyncio.to_thread(_require_chat_owner, request, row["tier"], row["name"])
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


def _signed_kind(principal: str | None) -> str:
    """Provenienza di un'identità FIRMATA (session token verificato dalla CA).

    Solo per chiamanti autenticati: classificare un nome che il chiamante ha
    DICHIARATO è esattamente il finding 1 della review di #221 — chi arriva
    senza firma dicendosi `davide` non deve poter spegnere il segnale. Se il
    modulo non si importa, `external`: nel dubbio non si concede niente.

    Il degrado è fail-closed ma NON silenzioso: un import rotto in modo
    permanente farebbe entrare `external` anche il caller firmato, e un
    fail-closed che nessuno vede è indistinguibile dal funzionamento normale
    finché qualcuno non si chiede perché ogni turno sembra venire da fuori.
    """
    try:
        from ..api.channels import _inbound_kind
        return _inbound_kind(principal)
    except Exception as e:  # noqa: BLE001
        # Senza il nome: `_safe_name` vive nel modulo che non si è importato,
        # e un nome non sanitizzato in un log è la riga fabbricata di sempre.
        # Qui interessa che il degrado si veda, non chi l'ha innescato.
        LOG.warning("provenienza non calcolabile (%s): l'innesco entra "
                    "come external", e)
        return "external"


async def _queue_turn(tier: str, name: str, text: str, principal: str,
                      responder: str | None = None, *, kind: str,
                      author: str | None = None) -> bool:
    """Accoda il turno dichiarando DA DOVE arriva il testo (issue #221).

    `kind` è obbligatorio e keyword-only: la provenienza la stabilisce
    l'endpoint, che è l'unico a sapere con quale autorità ha autenticato la
    richiesta, e non si ricostruisce qui dal nome del principal — un nome non è
    una firma. Obbligatorio perché aggiungere una porta d'ingresso deve
    costringere a rispondere alla domanda, non a ereditare un default.

    `author` = chi si mostra al turno, se diverso dal principal tecnico (es.
    l'autore configurato dell'hook, mentre il principal è `hook`).
    """
    try:
        from ..api.channels import (run_topic_turn, _spawn_bg, _safe_name,
                                    _untrusted_trigger_directive)
        topic = await topics_client.async_open_topic(tier, name)
        meta = (topic or {}).get("meta", {})
        chi = _safe_name(author or principal)
        _spawn_bg(run_topic_turn(
            tier, name, meta, trigger_text=text, principal_hint=principal,
            responder_hint=responder, trigger_author=chi, trigger_kind=kind,
            directive=(_untrusted_trigger_directive(chi)
                       if kind == "external" else "")))
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
    topic = await topics_client.async_open_topic(tier, name)
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
    topic = await topics_client.async_open_topic(tier, name)
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
        await topics_client.async_post_message(tier, name, author=caller, text=text, kind="ai")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"post_message fallita: {e}") from e
    # Il caller qui è FIRMATO (`_principal_from_request`, mai il body): la sua
    # provenienza si può classificare. Un partecipante `proxy` che invoca resta
    # `external`, ed è il caso della issue.
    triggered = await _queue_turn(tier, name, text, caller, responder=caller,
                                  kind=_signed_kind(caller))
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
        await topics_client.async_post_message(
            tier, name, author=row["author"], text=text, kind="external")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"post_message fallita: {e}") from e

    # PROVENIENZA (issue #221). Un webhook è per COSTRUZIONE un sistema terzo:
    # questa porta è autorizzata dal solo segreto dell'hook, nessuna identità
    # firmata entra da qui, quindi `external` è una costante e non il risultato
    # di una lookup. Ricostruirlo dal nome renderebbe la classificazione
    # dipendente da come l'owner ha chiamato l'hook: un `author` che coincide con
    # una persona registrata farebbe entrare il payload come `human`.
    triggered = await _queue_turn(tier, name, text, "hook", kind="external",
                                  author=row["author"])
    db.record_event(hid, "ok", source=src, authority="untrusted")
    return {"ok": True, "injected": True, "triggered": triggered,
            "authority": "untrusted", "principal": None}
