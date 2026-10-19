#!/usr/bin/env python3
"""
Email client per Erre Claudia — multi-account
Supporta Gmail (demo), IONOS (owner@example.com), API relay (SMTP2GO)
Usa solo librerie standard Python (imaplib, smtplib, email, urllib)
Config: secrets/email_config.json

Vendored into google-pack from r-clodia/clodia-tools:vendor/email_client.py
(commit c1d6921bb463, clodia-platform#524): the Gmail (XOAUTH2) client of the
`mail-gmail` provider, run by gmail_core.py (google-pack's gateway component,
clodia-platform#558) with CLODIA_SECRETS_DIR set to a per-call temp dir. Keep
it identical to the gateway copy except for this note.
"""

import imaplib
import smtplib
import ssl
import email
import email.utils
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.header import decode_header
import os
import json
import base64
import argparse
import sys
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import urllib.parse

# Config in secrets/, non in tools/email/

__version__ = "1.0.0"
BASE_DIR = Path(__file__).parent
# Risoluzione portabile dei secret (container-aware), coerente con trello_client.
_WS = os.environ.get("CLODIA_WORKSPACE_ROOT")
_SECRETS_DIR = Path(
    os.environ.get("CLODIA_SECRETS_DIR")
    or (f"{_WS}/secrets" if _WS else str(BASE_DIR.parent.parent.parent / "secrets"))
)
CONFIG_FILE = _SECRETS_DIR / "email_config.json"

# Profili server predefiniti
SERVER_PROFILES = {
    "gmail": {
        "imap_server": "imap.gmail.com",
        "imap_port": 993,
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
    },
    "ionos": {
        "imap_server": "imap.ionos.it",
        "imap_port": 993,
        "smtp_server": "smtp.ionos.it",
        "smtp_port": 587,
    },
}


def load_config():
    """Carica configurazione multi-account da secrets/email_config.json"""
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE, "r") as f:
        data = json.load(f)
    # Compatibilità formato vecchio (single-account)
    if "accounts" not in data:
        return {
            "accounts": {"demo": data},
            "default": "demo",
        }
    return data


def save_config(config):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_account(config, account_name):
    """Restituisce la config del singolo account, con errore chiaro se mancante."""
    accounts = config.get("accounts", {})
    if account_name not in accounts:
        available = list(accounts.keys())
        raise ValueError(
            f"Account '{account_name}' non trovato. Disponibili: {available}"
        )
    acc = dict(accounts[account_name])
    acc["_name"] = account_name   # serve all'OAuth (lookup refresh token)
    return acc


def decode_mime_header(header):
    if header is None:
        return ""
    decoded_parts = decode_header(header)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


# ── OAuth2 (XOAUTH2) per Gmail — stdlib only (urllib) ─────────────────────
# Sostituisce l'app-password con l'auth OAuth: l'access token si ottiene dal
# refresh token salvato (connect_email.py). Client e token vivono in secrets/.
import time as _time

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_OAUTH_CLIENT_FILE = _SECRETS_DIR / "google_oauth_client.json"   # {client_id, client_secret}
_OAUTH_TOKENS_FILE = _SECRETS_DIR / "email_oauth_tokens.json"    # {<account>: {refresh_token, email}}
_ACCESS_CACHE: dict = {}  # {account: (access_token, exp_epoch)}


def load_oauth_client() -> dict:
    return json.loads(_OAUTH_CLIENT_FILE.read_text())


def load_oauth_tokens() -> dict:
    if _OAUTH_TOKENS_FILE.is_file():
        return json.loads(_OAUTH_TOKENS_FILE.read_text())
    return {}


def save_oauth_token(account: str, refresh_token: str, email: str) -> None:
    tokens = load_oauth_tokens()
    tokens[account] = {"refresh_token": refresh_token, "email": email}
    _OAUTH_TOKENS_FILE.write_text(json.dumps(tokens, indent=2))


def _post_token(data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = Request(_GOOGLE_TOKEN_URL, data=body,
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def get_access_token(account_name: str) -> str:
    """Access token valido per l'account (cache in-memory finché non scade)."""
    tok = _ACCESS_CACHE.get(account_name)
    if tok and tok[1] - 60 > _time.time():
        return tok[0]
    client = load_oauth_client()
    rec = load_oauth_tokens().get(account_name)
    if not rec:
        raise RuntimeError(f"nessun refresh token per '{account_name}' — esegui connect_email.py")
    res = _post_token({
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": rec["refresh_token"],
        "grant_type": "refresh_token",
    })
    access = res["access_token"]
    _ACCESS_CACHE[account_name] = (access, _time.time() + int(res.get("expires_in", 3600)))
    return access


def _xoauth2_raw(email: str, access_token: str) -> bytes:
    return f"user={email}\x01auth=Bearer {access_token}\x01\x01".encode()


def connect_imap(account):
    # Certificate AND hostname verified (clodia-platform#556): the default
    # context of imaplib/smtplib checks neither.
    imap = imaplib.IMAP4_SSL(account["imap_server"], account["imap_port"],
                             ssl_context=ssl.create_default_context())
    if account.get("auth") == "oauth2":
        access = get_access_token(account["_name"])
        # imaplib base64-encoda da sé la stringa restituita dall'authobject
        imap.authenticate("XOAUTH2", lambda _: _xoauth2_raw(account["email"], access))
    else:
        password = account.get("app_password") or account.get("password")
        imap.login(account["email"], password)
    return imap


def connect_smtp(account):
    server = smtplib.SMTP(account["smtp_server"], account["smtp_port"])
    server.starttls(context=ssl.create_default_context())
    if account.get("auth") == "oauth2":
        access = get_access_token(account["_name"])
        b64 = base64.b64encode(_xoauth2_raw(account["email"], access)).decode()
        code, resp = server.docmd("AUTH", "XOAUTH2 " + b64)
        if code != 235:
            raise smtplib.SMTPAuthenticationError(code, resp)
    else:
        password = account.get("app_password") or account.get("password")
        smtp_user = account.get("smtp_user", account["email"])
        server.login(smtp_user, password)
    return server


# Nomi comuni della cartella "Inviati" (fallback se manca il flag SPECIAL-USE \Sent).
_SENT_FALLBACKS = ["Sent", "Posta inviata", "Sent Items", "Sent Messages",
                   "INBOX.Sent", "Elementi inviati", "Éléments envoyés", "Gesendet"]


def _find_sent_folder(imap):
    """Determina la cartella Inviati del server: prima via flag SPECIAL-USE
    `\\Sent` (robusto, indipendente da lingua/provider — es. IONOS = "Posta
    inviata"), poi per nome fra i fallback comuni. None se non determinabile."""
    try:
        status, lines = imap.list()
    except Exception:  # noqa: BLE001
        return None
    if status != "OK" or not lines:
        return None
    parsed = []  # (flags_lower, name)
    for raw in lines:
        s = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        flags = s[s.find("(") + 1:s.find(")")].lower() if "(" in s and ")" in s else ""
        # formato LIST: (flags) "sep" name → togli flags e separator, resta il nome
        rest = s[s.find(")") + 1:].strip() if ")" in s else s
        if rest.startswith('"'):                    # separator quotato es. "/"
            j = rest.find('"', 1)
            rest = rest[j + 1:].strip() if j != -1 else rest
        elif " " in rest:                           # separator NIL/non quotato
            rest = rest.split(None, 1)[1]
        name = rest.strip().strip('"')              # nome, eventualmente quotato
        if name:
            parsed.append((flags, name))
    for flags, name in parsed:  # 1) flag \Sent (autoritativo)
        if "\\sent" in flags:
            return name
    names = {n for _, n in parsed}
    for cand in _SENT_FALLBACKS:  # 2) nome comune realmente esistente
        if cand in names:
            return cand
    return None


def _save_to_sent(account, msg_bytes):
    """Salva una copia del messaggio nella cartella Inviati via IMAP APPEND
    (necessario per i provider non-Gmail: SMTP invia ma non archivia). La
    cartella è quella esplicita (`sent_folder`) o auto-rilevata via \\Sent.
    Ritorna il nome cartella usato, o None se non riuscito."""
    try:
        imap = connect_imap(account)
        try:
            sent = account.get("sent_folder") or _find_sent_folder(imap)
            if not sent:
                return None
            folder_arg = f'"{sent}"' if " " in sent else sent
            imap.append(folder_arg, None, None, msg_bytes)
            return sent
        finally:
            imap.logout()
    except Exception:  # noqa: BLE001 — Gmail archivia già da sé; non bloccare l'invio
        return None


def list_folders(account):
    imap = connect_imap(account)
    status, folders = imap.list()
    imap.logout()
    result = []
    for folder in folders:
        folder_str = folder.decode() if isinstance(folder, bytes) else folder
        result.append(folder_str)
    return result


def list_emails(account, folder="INBOX", limit=10):
    imap = connect_imap(account)
    imap.select(folder, readonly=True)
    status, messages = imap.search(None, "ALL")
    email_ids = messages[0].split()
    email_ids = email_ids[-limit:] if len(email_ids) > limit else email_ids
    email_ids = email_ids[::-1]

    result = []
    for eid in email_ids:
        status, msg_data = imap.fetch(eid, "(RFC822.HEADER)")
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])
                result.append({
                    "id": eid.decode(),
                    "from": decode_mime_header(msg["From"]),
                    "to": decode_mime_header(msg["To"]),
                    "subject": decode_mime_header(msg["Subject"]),
                    "date": msg["Date"],
                    "message_id": msg["Message-ID"],
                })
    imap.logout()
    return result


def read_email(account, email_id, folder="INBOX"):
    imap = connect_imap(account)
    imap.select(folder, readonly=True)
    status, msg_data = imap.fetch(
        email_id.encode() if isinstance(email_id, str) else email_id, "(RFC822)"
    )

    result = None
    for response_part in msg_data:
        if isinstance(response_part, tuple):
            msg = email.message_from_bytes(response_part[1])
            body = ""
            attachments = []

            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    content_disposition = str(part.get("Content-Disposition"))
                    if "attachment" in content_disposition:
                        filename = part.get_filename()
                        if filename:
                            attachments.append(decode_mime_header(filename))
                    elif content_type == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or "utf-8"
                            body = payload.decode(charset, errors="replace")
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace")

            result = {
                "id": email_id,
                "from": decode_mime_header(msg["From"]),
                "to": decode_mime_header(msg["To"]),
                "subject": decode_mime_header(msg["Subject"]),
                "date": msg["Date"],
                "message_id": msg["Message-ID"],
                "references": msg.get("References", ""),
                "in_reply_to": msg.get("In-Reply-To", ""),
                "body": body,
                "attachments": attachments,
            }
    imap.logout()
    return result


def search_emails(account, query, folder="INBOX", limit=20):
    imap = connect_imap(account)
    imap.select(folder, readonly=True)
    status, messages = imap.search(None, query)
    email_ids = messages[0].split()
    email_ids = email_ids[-limit:] if len(email_ids) > limit else email_ids
    email_ids = email_ids[::-1]

    result = []
    for eid in email_ids:
        status, msg_data = imap.fetch(eid, "(RFC822.HEADER)")
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])
                result.append({
                    "id": eid.decode(),
                    "from": decode_mime_header(msg["From"]),
                    "to": decode_mime_header(msg["To"]),
                    "subject": decode_mime_header(msg["Subject"]),
                    "date": msg["Date"],
                    "message_id": msg["Message-ID"],
                })
    imap.logout()
    return result


def _part_bytes(part):
    """Byte di una parte MIME, contenitori message/rfc822 inclusi.

    `get_payload(decode=True)` torna `None` per un contenitore (un `.eml`
    allegato): il suo payload è una LISTA con dentro il sotto-messaggio, non
    bytes con un Content-Transfer-Encoding da sciogliere. Scartare quel `None`
    faceva sparire l'allegato pur essendo elencato da `read_email`, che il nome
    lo legge senza mai decodificare (issue #400). L'unico modo di ottenere i
    byte del .eml è riserializzare il sotto-messaggio.

    Ritorna `None` solo quando la parte non ha davvero contenuto: chi chiama
    distingue ancora «non estraibile» da «vuoto».
    """
    payload = part.get_payload(decode=True)
    if payload is not None:
        return payload
    raw = part.get_payload(decode=False)
    if isinstance(raw, list):
        # Un message/rfc822 ne contiene esattamente uno; il join copre senza
        # perdite il caso teorico di più sotto-messaggi.
        serialized = b"\n".join(
            sub.as_bytes() if hasattr(sub, "as_bytes") else str(sub).encode("utf-8")
            for sub in raw
        )
        return serialized or None
    if isinstance(raw, str):
        return raw.encode("utf-8")
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    return None


def download_attachments(account, email_id, output_dir=".", folder="INBOX"):
    imap = connect_imap(account)
    imap.select(folder, readonly=True)
    status, msg_data = imap.fetch(
        email_id.encode() if isinstance(email_id, str) else email_id, "(RFC822)"
    )

    downloaded = []
    for response_part in msg_data:
        if isinstance(response_part, tuple):
            msg = email.message_from_bytes(response_part[1])
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                if part.get("Content-Disposition") is None:
                    continue
                filename = part.get_filename()
                if filename:
                    decoded_filename = decode_mime_header(filename)
                    filepath = Path(output_dir) / decoded_filename
                    payload = _part_bytes(part)
                    if payload is None:
                        continue
                    with open(filepath, "wb") as f:
                        f.write(payload)
                    downloaded.append({
                        "filename": decoded_filename,
                        "path": str(filepath),
                        "size": filepath.stat().st_size,
                    })
    imap.logout()
    return downloaded



def get_eml(account, email_id, folder="INBOX"):
    """Il messaggio intero, RFC822, in base64 (copia .eml del corriere, #576)."""
    imap = connect_imap(account)
    imap.select(folder, readonly=True)
    status, msg_data = imap.fetch(
        email_id.encode() if isinstance(email_id, str) else email_id, "(RFC822)"
    )
    for response_part in msg_data or []:
        if isinstance(response_part, tuple):
            raw = response_part[1]
            mid = email.message_from_bytes(raw).get("Message-ID", "")
            imap.logout()
            return {"id": str(email_id), "message_id": str(mid).strip(),
                    "size": len(raw), "encoding": "base64",
                    "data": base64.b64encode(raw).decode("ascii")}
    imap.logout()
    return {"error": f"messaggio {email_id} non trovato"}


def get_attachment(account, email_id, filename, folder="INBOX"):
    """Ritorna il contenuto base64 di un singolo allegato (per nome file)."""
    imap = connect_imap(account)
    imap.select(folder, readonly=True)
    status, msg_data = imap.fetch(
        email_id.encode() if isinstance(email_id, str) else email_id, "(RFC822)"
    )
    target = None
    for response_part in msg_data:
        if isinstance(response_part, tuple):
            msg = email.message_from_bytes(response_part[1])
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                if part.get("Content-Disposition") is None:
                    continue
                fn = part.get_filename()
                if fn and decode_mime_header(fn) == filename:
                    payload = _part_bytes(part)
                    if payload is None:
                        continue
                    target = {
                        "filename": filename,
                        "size": len(payload),
                        "content_type": part.get_content_type(),
                        "encoding": "base64",
                        "data": base64.b64encode(payload).decode("ascii"),
                    }
                    break
    imap.logout()
    if target is None:
        raise ValueError(f"allegato non trovato: {filename}")
    return target


def reply_email(account, email_id, body, folder="INBOX", cc=None, attachments=None):
    """Rispondi a un'email mantenendo il threading (In-Reply-To + References)."""
    original = read_email(account, email_id, folder)
    if not original:
        raise ValueError(f"Email {email_id} non trovata in {folder}")

    # Costruisci subject con Re: se non presente
    orig_subject = original["subject"] or ""
    subject = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"

    # Destinatario = mittente originale: UN indirizzo, il primo, quello che
    # la PDP giudica (`address_of`). Un `From` con più indirizzi
    # («Ok <ok@vetted.it>, evil@x.com») mandava la risposta a tutti mentre la
    # PDP ne valutava uno solo (clodia-platform#559, review).
    _parsed = [a for _, a in email.utils.getaddresses([original["from"] or ""]) if "@" in a]
    to = _parsed[0] if _parsed else ""
    if not to:
        raise ValueError("mittente originale senza un indirizzo valido: risposta non inviata")

    # Threading headers
    orig_message_id = original.get("message_id", "")
    orig_references = original.get("references", "") or ""
    # References = vecchi references + message-id originale
    references = f"{orig_references} {orig_message_id}".strip()

    if account.get("smtp_backend") == "api":
        return _reply_email_api(account, to, subject, body, orig_message_id, references, cc, attachments)

    msg = MIMEMultipart()
    display_name = account.get("display_name")
    msg["From"] = f'"{display_name}" <{account["email"]}>' if display_name else account["email"]
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc if isinstance(cc, str) else ", ".join(cc)
    if orig_message_id:
        msg["In-Reply-To"] = orig_message_id
    if references:
        msg["References"] = references
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if attachments:
        for filepath in attachments:
            path = Path(filepath)
            if path.exists():
                with open(path, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    # RFC 2231 parameter (clodia-platform#557): a non-ASCII
                    # name in a raw header arrived as an unnamed attachment.
                    part.add_header("Content-Disposition", "attachment",
                                    filename=path.name)
                    msg.attach(part)

    with connect_smtp(account) as server:
        server.send_message(msg)
    msg_bytes = msg.as_bytes()
    _save_to_sent(account, msg_bytes)  # copia in Inviati (cartella auto-rilevata via \Sent)

    return {"to": to, "subject": subject}


def _reply_email_api(account, to, subject, body, in_reply_to, references, cc=None, attachments=None):
    """Reply via API REST con header di threading custom."""
    api_url = account["api_url"]
    api_key_file = account.get("api_key_file")
    api_key = account.get("api_key")
    if api_key_file and not api_key:
        key_path = BASE_DIR.parent.parent.parent / api_key_file
        api_key = key_path.read_text().strip()

    display_name = account.get("display_name")
    sender = f"{display_name} <{account['email']}>" if display_name else account["email"]

    to_list = [to] if isinstance(to, str) else to
    payload = {
        "api_key": api_key,
        "to": to_list,
        "sender": sender,
        "subject": subject,
        "text_body": body,
    }
    if cc:
        payload["cc"] = [cc] if isinstance(cc, str) else cc

    # Custom headers per threading
    custom_headers = {}
    if in_reply_to:
        custom_headers["In-Reply-To"] = in_reply_to
    if references:
        custom_headers["References"] = references
    if custom_headers:
        payload["custom_headers"] = [
            {"header": k, "value": v} for k, v in custom_headers.items()
        ]

    if attachments:
        att_list = []
        for filepath in attachments:
            path = Path(filepath)
            if path.exists():
                with open(path, "rb") as f:
                    data = base64.b64encode(f.read()).decode()
                    att_list.append({"filename": path.name, "fileblob": data, "mimetype": "application/octet-stream"})
        if att_list:
            payload["attachments"] = att_list

    data = json.dumps(payload).encode("utf-8")
    req = Request(api_url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req) as resp:
            result = json.loads(resp.read().decode())
            if result.get("data", {}).get("failed", 0) > 0:
                raise RuntimeError(f"API send failed: {result}")
    except HTTPError as e:
        raise RuntimeError(f"API error {e.code}: {e.read().decode()}")

    return {"to": to, "subject": subject}


def send_email_api(account, to, subject, body, cc=None, attachments=None):
    """Invio email via API REST (es. SMTP2GO). Nessuna dipendenza SMTP/IMAP."""
    api_url = account["api_url"]
    api_key_file = account.get("api_key_file")
    api_key = account.get("api_key")
    if api_key_file and not api_key:
        key_path = BASE_DIR.parent.parent.parent / api_key_file
        api_key = key_path.read_text().strip()

    display_name = account.get("display_name")
    sender = f"{display_name} <{account['email']}>" if display_name else account["email"]

    to_list = [to] if isinstance(to, str) else to
    payload = {
        "api_key": api_key,
        "to": to_list,
        "sender": sender,
        "subject": subject,
        "text_body": body,
    }
    if cc:
        payload["cc"] = [cc] if isinstance(cc, str) else cc

    if attachments:
        att_list = []
        for filepath in attachments:
            path = Path(filepath)
            if path.exists():
                with open(path, "rb") as f:
                    data = base64.b64encode(f.read()).decode()
                    att_list.append({"filename": path.name, "fileblob": data, "mimetype": "application/octet-stream"})
        if att_list:
            payload["attachments"] = att_list

    data = json.dumps(payload).encode("utf-8")
    req = Request(api_url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req) as resp:
            result = json.loads(resp.read().decode())
            if result.get("data", {}).get("failed", 0) > 0:
                raise RuntimeError(f"API send failed: {result}")
    except HTTPError as e:
        raise RuntimeError(f"API error {e.code}: {e.read().decode()}")

    return True


def send_email(account, to, subject, body, cc=None, attachments=None):
    # Se l'account usa un backend API (es. SMTP2GO), delega a send_email_api
    if account.get("smtp_backend") == "api":
        return send_email_api(account, to, subject, body, cc, attachments)

    msg = MIMEMultipart()
    display_name = account.get("display_name")
    msg["From"] = f'"{display_name}" <{account["email"]}>' if display_name else account["email"]
    msg["To"] = to if isinstance(to, str) else ", ".join(to)
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc if isinstance(cc, str) else ", ".join(cc)
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if attachments:
        for filepath in attachments:
            path = Path(filepath)
            if path.exists():
                with open(path, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    # RFC 2231 parameter (clodia-platform#557): a non-ASCII
                    # name in a raw header arrived as an unnamed attachment.
                    part.add_header("Content-Disposition", "attachment",
                                    filename=path.name)
                    msg.attach(part)

    with connect_smtp(account) as server:
        server.send_message(msg)
    msg_bytes = msg.as_bytes()
    _save_to_sent(account, msg_bytes)  # copia in Inviati (cartella auto-rilevata via \Sent)
    return True


def main():
    parser = argparse.ArgumentParser(description="Email client per Erre Claudia — multi-account")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--policy", action="store_true", help="Mostra la policy del tool ed esci")
    parser.add_argument(
        "--account",
        default=None,
        help="Account da usare (default: valore 'default' in config). Es: demo, studio",
    )
    subparsers = parser.add_subparsers(dest="command", help="Comandi disponibili")

    # configure
    config_parser = subparsers.add_parser("configure", help="Configura un account")
    config_parser.add_argument("--email", required=True, help="Indirizzo email")
    config_parser.add_argument("--password", help="Password (o app password per Gmail)")
    config_parser.add_argument("--password-file", help="File contenente la password")
    config_parser.add_argument(
        "--profile",
        choices=list(SERVER_PROFILES.keys()),
        help="Profilo server predefinito (gmail, ionos)",
    )
    config_parser.add_argument("--imap-server", help="Server IMAP (override profilo)")
    config_parser.add_argument("--imap-port", type=int, help="Porta IMAP (default: 993)")
    config_parser.add_argument("--smtp-server", help="Server SMTP (override profilo)")
    config_parser.add_argument("--smtp-port", type=int, help="Porta SMTP (default: 587)")
    config_parser.add_argument("--display-name", help='Nome visualizzato dal destinatario (es: "the owner, Ing.")')
    config_parser.add_argument("--set-default", action="store_true", help="Imposta come account predefinito")

    # folders
    subparsers.add_parser("folders", help="Lista cartelle")

    # list
    list_parser = subparsers.add_parser("list", help="Lista email")
    list_parser.add_argument("--folder", default="INBOX")
    list_parser.add_argument("--limit", type=int, default=10)

    # read
    read_parser = subparsers.add_parser("read", help="Leggi email")
    read_parser.add_argument("email_id")
    read_parser.add_argument("--folder", default="INBOX")

    # search
    search_parser = subparsers.add_parser("search", help="Cerca email")
    search_parser.add_argument("query", help='Query IMAP (es: FROM "test@example.com")')
    search_parser.add_argument("--folder", default="INBOX")
    search_parser.add_argument("--limit", type=int, default=20)

    # send
    send_parser = subparsers.add_parser("send", help="Invia email")
    send_parser.add_argument("--to", required=True)
    send_parser.add_argument("--subject", required=True)
    send_parser.add_argument("--body", required=True)
    send_parser.add_argument("--cc")
    send_parser.add_argument("--attachment", action="append")

    # reply
    reply_parser = subparsers.add_parser("reply", help="Rispondi a un'email (mantiene threading)")
    reply_parser.add_argument("email_id", help="ID dell'email a cui rispondere")
    reply_parser.add_argument("--body", required=True, help="Corpo della risposta")
    reply_parser.add_argument("--folder", default="INBOX")
    reply_parser.add_argument("--cc")
    reply_parser.add_argument("--attachment", action="append")

    # download-attachments
    download_parser = subparsers.add_parser("download-attachments", help="Scarica allegati")
    download_parser.add_argument("email_id")
    download_parser.add_argument("--output", default=".")
    download_parser.add_argument("--folder", default="INBOX")

    ga_parser = subparsers.add_parser("get-attachment", help="Contenuto base64 di un allegato")
    ga_parser.add_argument("email_id")
    ga_parser.add_argument("--filename", required=True)
    ga_parser.add_argument("--folder", default="INBOX")

    eml_parser = subparsers.add_parser("get-eml", help="Messaggio intero (RFC822) in base64")
    eml_parser.add_argument("email_id")
    eml_parser.add_argument("--folder", default="INBOX")

    # accounts
    subparsers.add_parser("accounts", help="Lista account configurati")

    args = parser.parse_args()
    if args.policy:
        policy_file = Path(__file__).parent / "POLICY.md"
        if policy_file.exists():
            print(policy_file.read_text())
        else:
            print("POLICY.md non trovato.")
        sys.exit(0)

    # ── configure ────────────────────────────────────────────────────────────
    if args.command == "configure":
        if args.password_file:
            with open(args.password_file, "r") as f:
                password = f.read().strip()
        elif args.password:
            password = args.password
        else:
            print(json.dumps({"error": "Specificare --password o --password-file"}))
            return

        # Determina nome account dall'email (parte prima della @)
        account_name = args.account or args.email.split("@")[0].replace(".", "_")

        # Parte con il profilo predefinito se specificato
        servers = {}
        if args.profile:
            servers = SERVER_PROFILES[args.profile].copy()
        if args.imap_server:
            servers["imap_server"] = args.imap_server
        if args.imap_port:
            servers["imap_port"] = args.imap_port
        if args.smtp_server:
            servers["smtp_server"] = args.smtp_server
        if args.smtp_port:
            servers["smtp_port"] = args.smtp_port

        # Default a Gmail se nessun server specificato (compatibilità)
        if not servers:
            servers = SERVER_PROFILES["gmail"].copy()

        account_entry = {"email": args.email, **servers}
        if args.display_name:
            account_entry["display_name"] = args.display_name
        # Gmail usa "app_password", gli altri "password"
        if servers.get("imap_server") == "imap.gmail.com":
            account_entry["app_password"] = password
        else:
            account_entry["password"] = password

        config = load_config()
        if "accounts" not in config:
            config = {"accounts": {}, "default": account_name}
        config["accounts"][account_name] = account_entry
        if args.set_default or len(config["accounts"]) == 1:
            config["default"] = account_name

        save_config(config)
        print(json.dumps({
            "status": "ok",
            "message": f"Account '{account_name}' configurato",
            "default": config["default"],
        }))
        return

    # ── accounts ─────────────────────────────────────────────────────────────
    if args.command == "accounts":
        config = load_config()
        accounts = config.get("accounts", {})
        default = config.get("default", "")
        result = []
        for name, acc in accounts.items():
            result.append({
                "name": name,
                "email": acc.get("email"),
                "imap": acc.get("imap_server"),
                "default": name == default,
            })
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    # ── altri comandi: carica config e seleziona account ─────────────────────
    config = load_config()
    if not config or not config.get("accounts"):
        print(json.dumps({"error": "Nessun account configurato. Usa: configure --email ... --password ... --profile ..."}))
        return

    account_name = args.account or config.get("default") or list(config["accounts"].keys())[0]
    try:
        account = get_account(config, account_name)
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        return

    if args.command == "folders":
        print(json.dumps(list_folders(account), indent=2, ensure_ascii=False))

    elif args.command == "list":
        print(json.dumps(list_emails(account, args.folder, args.limit), indent=2, ensure_ascii=False))

    elif args.command == "read":
        result = read_email(account, args.email_id, args.folder)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "search":
        result = search_emails(account, args.query, args.folder, args.limit)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "send":
        send_email(account, args.to, args.subject, args.body, cc=args.cc, attachments=args.attachment)
        print(json.dumps({"status": "ok", "message": f"Email inviata da {account['email']}"}))

    elif args.command == "reply":
        result = reply_email(account, args.email_id, args.body, folder=args.folder, cc=args.cc, attachments=args.attachment)
        print(json.dumps({"status": "ok", "message": f"Reply inviato da {account['email']}", "to": result["to"], "subject": result["subject"]}))

    elif args.command == "get-eml":
        print(json.dumps(get_eml(account, args.email_id, args.folder), ensure_ascii=False))

    elif args.command == "get-attachment":
        result = get_attachment(account, args.email_id, args.filename, args.folder)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == "download-attachments":
        result = download_attachments(account, args.email_id, args.output, args.folder)
        if result:
            print(json.dumps({"status": "ok", "message": f"{len(result)} allegati scaricati", "files": result}, indent=2, ensure_ascii=False))
        else:
            print(json.dumps({"status": "ok", "message": "Nessun allegato trovato"}))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
