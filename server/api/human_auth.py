"""Login UMANO (F2a) — identifica il principal dalla FIRMA, non dal nome.

L'utente fornisce SOLO la propria masterkey (recovery): il browser firma un
token; questo endpoint prova a verificarlo contro i cert di tutti i principal
`type: human` e ritorna quello che combacia (+ display_name/role). Una chiave
che non corrisponde a nessun cert viene rifiutata (401) → niente login con nomi
inventati. Il nome NON viene chiesto: lo deriva il server dal cert.
"""
from __future__ import annotations

import hashlib
import json
import logging
import smtplib
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..colony import pki
from ..config import data_path
from . import admin
from .agents import _principal_from_request

router = APIRouter()
LOG = logging.getLogger("agent-server.api.human_auth")
AGENTS_DIR = data_path("agents")
# Config dei canali di notifica all'admin (clonabile, per-istanza). Se assente o
# senza canali, le richieste di certificato si perdono (loggato).
NOTIFY_CONFIG = data_path("admin_notify.json")
# Richieste di certificato PENDENTI (persistite): l'admin le approva con un click
# senza re-inserire nome/pubkey (già nella richiesta dell'utente). Sotto data/
# perché nel deploy pristine è una dir MONTATA (persiste al recreate); /datadir
# monta solo subpath specifici, non l'intera root.
CERT_REQ_DIR = data_path("data") / "cert-requests"


def _require_admin(request: Request) -> None:
    if not admin.is_admin(_principal_from_request(request)):
        raise HTTPException(403, "operazione riservata agli admin")


def _human_principals() -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    if not AGENTS_DIR.is_dir():
        return out
    for d in sorted(AGENTS_DIR.iterdir()):
        ay = d / "agent.yaml"
        if not ay.is_file():
            continue
        try:
            meta = yaml.safe_load(ay.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if meta.get("type") == "human":
            out.append((d.name, meta))
    return out


class TokenBody(BaseModel):
    token: str


@router.post("/clodia/whoami")
async def whoami(body: TokenBody) -> dict:
    """Identifica l'utente dal token firmato (prova i cert dei principal umani)."""
    reasons = []
    for name, meta in _human_principals():
        try:
            pki.verify_token_against(body.token, name)
        except Exception as e:  # noqa: BLE001 — firma non per questo principal/scaduta
            reasons.append(f"{name}: {e}")
            continue
        LOG.info("login umano: principal '%s' identificato", name)
        return {
            "principal": name,
            "display_name": meta.get("display_name"),
            "role": meta.get("role"),
        }
    LOG.warning("whoami 401 — nessun match. Ragioni: %s", "; ".join(reasons) or "(nessun principal human)")
    raise HTTPException(401, "nessun principal corrisponde a questa chiave (o token scaduto)")


def _notify_admin(text: str) -> list[str]:
    """Invia `text` ai canali admin configurati (telegram + email se presenti).
    Ritorna i canali usati. Se nessun canale è configurato → [] (richiesta persa,
    loggato). Best-effort: un canale che fallisce non blocca gli altri."""
    if not NOTIFY_CONFIG.is_file():
        LOG.warning("cert-request: nessun canale admin configurato → richiesta PERSA")
        return []
    try:
        cfg = json.loads(NOTIFY_CONFIG.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        LOG.warning("admin_notify.json illeggibile: %s", e)
        return []
    sent: list[str] = []
    tg = cfg.get("telegram") or {}
    if tg.get("bot_token") and tg.get("chat_id"):
        try:
            data = urllib.parse.urlencode({"chat_id": tg["chat_id"], "text": text}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage", data=data)
            urllib.request.urlopen(req, timeout=15)
            sent.append("telegram")
        except Exception as e:  # noqa: BLE001
            LOG.warning("notifica telegram fallita: %s", e)
    em = cfg.get("email") or {}
    if em.get("smtp_host") and em.get("to"):
        try:
            msg = EmailMessage()
            msg["Subject"] = "Clodia — richiesta di certificato"
            msg["From"] = em.get("from") or em.get("user") or em["to"]
            msg["To"] = em["to"]
            msg.set_content(text)
            with smtplib.SMTP(em["smtp_host"], int(em.get("smtp_port", 587)), timeout=20) as s:
                s.starttls()
                if em.get("user"):
                    s.login(em["user"], em.get("password", ""))
                s.send_message(msg)
            sent.append("email")
        except Exception as e:  # noqa: BLE001
            LOG.warning("notifica email fallita: %s", e)
    if not sent:
        LOG.warning("cert-request: canali configurati ma invio fallito → richiesta PERSA")
    return sent


class CertRequest(BaseModel):
    pubkey: str
    name: str | None = None
    contact: str | None = None


@router.post("/clodia/cert-request")
async def cert_request(body: CertRequest) -> dict:
    """Un nuovo utente (non ancora registrato) chiede all'admin di emettere il
    certificato per la propria pubkey. NON crea nulla: notifica solo l'admin
    (telegram/email), che poi crea l'agent umano incollando la pubkey."""
    pub = (body.pubkey or "").strip()
    if "BEGIN PUBLIC KEY" not in pub:
        raise HTTPException(400, "pubkey non valida (attesa PEM 'BEGIN PUBLIC KEY')")
    who = (body.name or "(anonimo)").strip()
    contact = (body.contact or "").strip()
    text = ("🔑 Clodia — nuova richiesta di accesso\n"
            f"Nome: {who}\n"
            + (f"Contatto: {contact}\n" if contact else "")
            + "Per approvare: crea un agent umano con questa pubkey e assegna la clearance.\n\n"
            + pub)
    sent = _notify_admin(text)
    # Persisti la richiesta così l'admin la approva con un click (nome+pubkey già
    # presenti, niente re-inserimento). id deterministico dalla pubkey → ri-richieste
    # dello stesso utente non duplicano.
    rid = hashlib.sha256(pub.encode()).hexdigest()[:16]
    try:
        CERT_REQ_DIR.mkdir(parents=True, exist_ok=True)
        (CERT_REQ_DIR / f"{rid}.json").write_text(json.dumps({
            "id": rid, "name": who, "contact": contact, "pubkey": pub,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        LOG.warning("cert-request: persistenza fallita: %s", e)
    LOG.info("cert-request da '%s' → canali: %s", who, sent or "nessuno (persa)")
    return {"submitted": True, "notified": sent}


@router.get("/api/cert-requests")
async def list_cert_requests(request: Request) -> list[dict]:
    """Richieste di accesso pendenti (solo admin). Nome e pubkey sono già qui:
    l'admin approva senza re-inserirli."""
    _require_admin(request)
    out = []
    if CERT_REQ_DIR.is_dir():
        for f in sorted(CERT_REQ_DIR.glob("*.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    out.sort(key=lambda r: r.get("ts") or "")
    return out


@router.delete("/api/cert-requests/{rid}")
async def delete_cert_request(rid: str, request: Request) -> dict:
    """Rimuove una richiesta pendente (dopo approvazione o per rifiuto). Solo admin."""
    _require_admin(request)
    safe = "".join(c for c in rid if c.isalnum())
    p = CERT_REQ_DIR / f"{safe}.json"
    if p.is_file():
        try:
            p.unlink()
        except OSError as e:
            raise HTTPException(500, f"rimozione fallita: {e}")
    return {"deleted": safe}
