"""Notifiche gate/END dei workflow verso l'owner (Telegram + email).

L'owner è l'agente umano dichiarato nel manifest del workflow (`wf_owner`),
con fallback su chi ha avviato il run. I contatti (`email`, `telegram`) stanno
sul suo record AgentSpec. Best-effort: una notifica fallita non blocca il run.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

from ..config import data_path

LOG = logging.getLogger("agent-server.workflows.notify")


def _webui_url() -> str:
    return (os.environ.get("CLODIA_WEBUI_URL") or "").rstrip("/")


def owner_contacts(run: dict) -> tuple[str, str | None, str | None]:
    """(owner_name, telegram, email) dell'owner del run (o del richiedente)."""
    from ..agents.loader import registry
    name = (run.get("wf_owner") or "").strip() or (run.get("requested_by") or "")
    try:
        spec = registry.get_by_name(name)
    except Exception:  # noqa: BLE001
        spec = None
    tg = getattr(spec, "telegram", None) if spec else None
    em = getattr(spec, "email", None) if spec else None
    return name, tg, em


def _send_telegram(chat_id: str, text: str) -> bool:
    try:
        from ..api.telegram_client import send
        send(str(chat_id), text)
        return True
    except Exception as e:  # noqa: BLE001
        LOG.warning("notifica telegram fallita: %s", str(e)[:120])
        return False


def _smtp_cfg() -> dict | None:
    import json
    p = data_path("admin_notify.json")
    if not p.is_file():
        return None
    try:
        return (json.loads(p.read_text(encoding="utf-8")) or {}).get("email") or None
    except Exception:  # noqa: BLE001
        return None


def _send_email(to: str, subject: str, text: str) -> bool:
    em = _smtp_cfg()
    if not (em and em.get("smtp_host") and to):
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = em.get("from") or em.get("user") or to
        msg["To"] = to
        msg.set_content(text)
        with smtplib.SMTP(em["smtp_host"], int(em.get("smtp_port", 587)), timeout=20) as s:
            s.starttls()
            if em.get("user"):
                s.login(em["user"], em.get("password", ""))
            s.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        LOG.warning("notifica email fallita: %s", str(e)[:120])
        return False


def _dispatch(run: dict, subject: str, text: str) -> list[str]:
    _, tg, em = owner_contacts(run)
    sent: list[str] = []
    if tg and _send_telegram(tg, f"{subject}\n\n{text}"):
        sent.append("telegram")
    if em and _send_email(em, subject, text):
        sent.append("email")
    if not sent:
        LOG.info("workflow %s: nessun canale di notifica per l'owner", run.get("id"))
    return sent


def notify_gate(run: dict, token: str, artefatto: str | None) -> None:
    """Notifica un gate: link firmato alla pagina di decisione + artefatto."""
    lane = run["stages"][run["current"]]["lane"]
    url = f"{_webui_url()}/gate/{token}" if _webui_url() else f"/gate/{token}"
    lines = [
        f"Workflow «{run['plugin']}/{run['workflow']}» — run «{run['title']}».",
        f"Decisione richiesta sullo stadio: {lane}.",
    ]
    if artefatto:
        lines.append(f"Da valutare: {artefatto}")
    lines += ["", f"Decidi qui (link monouso): {url}"]
    _dispatch(run, "🔔 Workflow: decisione richiesta", "\n".join(lines))


def notify_end(run: dict, artefatto: str | None) -> None:
    """Notifica la fine del run: link al risultato."""
    lines = [f"Workflow «{run['plugin']}/{run['workflow']}» — run «{run['title']}»: {run['status']}."]
    if artefatto:
        lines.append(f"Risultato: {artefatto}")
    elif _webui_url():
        lines.append(f"Dettaglio: {_webui_url()}/workflows/{run['plugin']}/{run['workflow']}")
    _dispatch(run, f"✅ Workflow completato: {run['title']}", "\n".join(lines))
