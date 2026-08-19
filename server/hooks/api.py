"""API dei Chat Hook.

CRUD riservato all'owner della chat (o admin di piattaforma), verificato dal
session token (principal firmato dalla CA). **Nessuna rotta pubblica**: l'ingress
`POST /hooks/{id}`, autorizzato dal solo segreto dell'hook, è stato chiuso con la
issue #300 (step 2 di clodia-platform#222) — 8 hook registrati sull'istanza viva,
`uses: 0` su tutti, e una porta da cui un terzo poteva iniettare un messaggio e
far partire un turno con un segreto accettato anche in query string.

Resta l'invocazione locale, che è un'altra cosa: non usa segreti, l'identità
arriva dal session token firmato ed è autorizzata solo per participant (più
l'eccezione messaggero).
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from . import db
from ..api import admin, topics_client
from ..api.agents import _principal_from_request

LOG = logging.getLogger("agent-server.hooks.api")

router = APIRouter()


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
    # Niente `path`/`url`: la rotta che quell'indirizzo nominava non esiste più
    # (#300). Pubblicarlo lo stesso significherebbe consegnare a un integratore
    # un endpoint che risponde 404 — un guasto che si scopre dopo, nel log di un
    # sistema terzo, ed è peggio di un campo assente.
    return {
        "hook": pub,
        "secret": secret,               # mostrato UNA sola volta
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
