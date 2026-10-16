"""The mailboxes of the instance: list, add, remove, test — the Tools page.

Moved from clodia-tools `server/tools_api.py` (gateway 2.109.0,
clodia-platform#558): `email_mailboxes`, `email_mailbox_add`,
`email_mailbox_remove` and the Tools card test `_test_mailboxes`/`_prova_smtp`.

These are SERVICES the gateway calls from its own routes
(`/tools/email/mailboxes`, `/tools/mailboxes/test`), not component routes:
the URLs, their authentication (`tools_api._authorized`: a human admin, the
trusted core, or the agent-server's service role during a pack import), the
profile's connector guard and the JSON parsing stay the gateway's, so a
switch flip changes neither who may manage mailboxes nor what the Tools page
calls. What a mailbox IS — the bundle's shape, the validation, the deposit in
the vault under `mailbox_<acct>`, the IMAP/SMTP login test — is here.

Removing a mailbox also withdraws its `inbox:`/`outbox:` entries from every
whitelist (clodia-platform#411). The whitelist is the gateway's own state: this
service removes the credential and returns the ADDRESS (read before the
removal — after it nothing says which entries were its); the gateway's route
then withdraws the entries itself (`egress.forget_mailbox`).
"""
from __future__ import annotations

import logging
import re

from . import mail
from ._host import vault

LOG = logging.getLogger("clodia-tools.component.comms-pack.mail")

_ACCOUNT = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")


def mailboxes_list() -> dict:
    """GET: the generic mailboxes (`mailbox_*`), names and status only."""
    rows = [row for row in mail.credential_diagnostics() if row["kind"] == "mailbox"]
    return {
        "mailboxes": sorted(row["account"] for row in rows if row["operational"]),
        "statuses": [
            {"account": row["account"], "operational": row["operational"],
             "missing": row["missing"], "error": row["error"]}
            for row in rows
        ],
    }


def mailbox_add(b: dict) -> dict:
    """POST body (already parsed by the gateway) → `{"status", "body"}`.

    Body: account, email, password, smtp_server, [imap_server, imap_port=993,
    smtp_port=587, display_name, sent_folder, smtp_user]. Deposited in the
    gateway's vault as `mailbox_<account>` (grant to clodia+ophelia)."""
    account = (b.get("account") or "").strip().lower()
    if not _ACCOUNT.fullmatch(account or ""):
        return {"status": 400, "body": {"error": "account non valido (a-z0-9_-)"}}
    # L'IMAP è OPZIONALE: esistono indirizzi che sono alias con SMTP e nessuna
    # casella dietro.
    required = ("email", "password", "smtp_server")
    if any(not (b.get(k) or "").strip() for k in required):
        return {"status": 400,
                "body": {"error": f"campi richiesti: {', '.join(required)}"}}
    cfg = {
        "email": b["email"].strip(),
        "password": b["password"],
        "smtp_server": b["smtp_server"].strip(),
        "smtp_port": int(b.get("smtp_port") or 587),
    }
    if (b.get("imap_server") or "").strip():
        cfg["imap_server"] = b["imap_server"].strip()
        cfg["imap_port"] = int(b.get("imap_port") or 993)
    for opt in ("display_name", "sent_folder", "smtp_user"):
        if (b.get(opt) or "").strip():
            cfg[opt] = b[opt].strip()
    vault.deposit(f"mailbox_{account}", cfg, cred_type="mailbox",
                  grant_agents=["clodia", "ophelia"])
    LOG.info("email_mailbox_add: casella '%s' depositata (%s)", account, cfg["email"])
    return {"status": 200, "body": {"account": account, "connected": True}}


def mailbox_remove(account: str) -> dict:
    """Remove `mailbox_<account>`; `{"removed", "email"}` — the address read
    BEFORE the removal, for the gateway to withdraw its whitelist entries."""
    indirizzo = mail.mailbox_address(account)
    removed = vault.remove(f"mailbox_{account}")
    return {"removed": removed, "email": indirizzo}


def _prova_smtp(b: dict, account: str) -> str:
    """Login SMTP, per le caselle che sanno solo spedire. L'esito in parole,
    mai il segreto né il messaggio grezzo del server."""
    import smtplib
    porta = int(b.get("smtp_port") or 587)
    utente = b.get("smtp_user") or b.get("email") or account
    pwd = b.get("password") or b.get("app_password") or ""
    try:
        # 465 è SMTPS implicito, 587 è STARTTLS.
        if porta == 465:
            s = smtplib.SMTP_SSL(b.get("smtp_server"), porta, timeout=15)
        else:
            s = smtplib.SMTP(b.get("smtp_server"), porta, timeout=15)
            s.starttls()
        try:
            s.login(utente, pwd)
            return "ok"
        finally:
            try:
                s.quit()
            except Exception:  # noqa: BLE001
                pass
    except smtplib.SMTPAuthenticationError:
        # Il rimedio dipende da COSA si è usato come utente: i relay (SMTP2GO,
        # Mailgun, SendGrid, Brevo) autenticano con un utente dedicato.
        if not (b.get("smtp_user") or "").strip():
            return ("autenticazione SMTP rifiutata usando l'indirizzo come utente. "
                    "Se il server è un relay (smtp2go, mailgun, sendgrid, brevo…) "
                    "l'utenza NON è l'indirizzo: compila «utente SMTP» con quella "
                    "del relay")
        return "autenticazione SMTP rifiutata (password sbagliata per l'utente indicato)"
    except Exception as e:  # noqa: BLE001
        return type(e).__name__


def test_mailboxes() -> dict:
    """Prova un login IMAP VERO per ogni casella configurata (SMTP per quelle
    di solo invio): «operativa» vuol dire «i campi ci sono», non «il login
    funziona» (clodia-platform#176). Il segreto non esce: l'esito per account."""
    import imaplib

    esiti, ok_tutti = [], True
    for nome in sorted(vault.store_names()):
        if not nome.startswith("mailbox_"):
            continue
        account = nome[len("mailbox_"):]
        try:
            b = vault.read_internal(nome)
        except Exception:  # noqa: BLE001
            esiti.append(f"{account}: credenziale illeggibile")
            ok_tutti = False
            continue
        pwd = b.get("password") or b.get("app_password") or ""
        if not pwd:
            esiti.append(f"{account}: manca la password")
            ok_tutti = False
            continue
        server = (b.get("imap_server") or "").strip()
        if not server:
            esiti.append(f"{account}: {_prova_smtp(b, account)} (solo invio)")
            if "ok" not in esiti[-1]:
                ok_tutti = False
            continue
        try:
            imap = imaplib.IMAP4_SSL(server, int(b.get("imap_port") or 993), timeout=15)
            try:
                imap.login(b.get("email") or account, pwd)
                esiti.append(f"{account}: ok")
            finally:
                try:
                    imap.logout()
                except Exception:  # noqa: BLE001
                    pass
        except imaplib.IMAP4.error:
            # Il messaggio del server IMAP non si riporta: su alcuni provider
            # contiene l'utenza.
            esiti.append(f"{account}: autenticazione IMAP rifiutata (password "
                         "sbagliata, oppure l'indirizzo è un alias di solo "
                         "invio: in quel caso togli il server IMAP)")
            ok_tutti = False
        except Exception as e:  # noqa: BLE001 — host irraggiungibile, TLS, timeout
            esiti.append(f"{account}: {type(e).__name__}")
            ok_tutti = False
    if not esiti:
        return {"ok": None, "detail": "nessuna casella configurata"}
    return {"ok": ok_tutti, "detail": " · ".join(esiti)}
