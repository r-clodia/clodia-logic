"""Argument handling of the eight `email.*` verbs: what the gateway's
dispatcher did in its `email.*` branches.

Moved from clodia-tools `server/main.py` (gateway 2.109.0, clodia-platform#558):
`_email_account`, `_agent_attachments`, `_topic_attachments`, the
`email.save_attachment` topic write with its taint, and `_reply_recipient`
(now the `pdp_arguments` service). The dispatcher calls `handle(name,
arguments)` at the point where it ran those branches — after the whitelist,
deny, unattended block, gates and the egress PDP — and taint and audit follow
it, as for a native verb.

What the host does here, as the native dispatcher did it:
- a path an agent names is resolved INSIDE its spawn's scratch
  (`host.scratch_path`, #461), never anywhere else the gateway can read;
- the bytes that leave are hashed into the call's audit record
  (`host.note_crossing`, kind `attachment`);
- topic files are read and written with the compartment check
  (`host.topics`), an archived attachment is `untrusted` and taints the
  channel.
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
from typing import Any, Callable

from . import _host
from . import mail

LOG = logging.getLogger("clodia-tools.component.comms-pack.mail")


# ── the account of a call ──────────────────────────────────────────────────

def _email_account(arguments: dict) -> str:
    """Account per una chiamata email.*: quello richiesto esplicitamente,
    altrimenti l'UNICO account esistente. Con zero o più account solleva un
    errore azionabile: nessun fallback verso mailbox inesistenti. Chi può
    davvero usare la casella lo decide la whitelist `inbox:`/`outbox:` al
    momento della chiamata (`mail._secrets_env`), non questa funzione."""
    agent = mail.agent_name()
    accounts = mail.available_accounts(agent)
    acct = (arguments.get("account") or "").strip()
    if acct:
        if acct in accounts:
            return acct
        # Fail closed for a Google account without google-pack, and say so:
        # comms-pack cannot even see a Google credential, so the account is
        # "not existing" here — the reason is google-pack, not a typo.
        why = _host.google_unavailable()
        raise ValueError(
            f"email: account '{acct}' non esistente. "
            f"Passa il parametro 'account' con uno di questi valori: {accounts}"
            + (f" ({why})" if why else "")
        )
    if len(accounts) == 1:
        return accounts[0]
    if not accounts:
        raise ValueError(
            "email: nessun account configurato. Configura una mailbox da "
            "Integrazioni → Email."
        )
    raise ValueError(
        "email: il parametro 'account' è obbligatorio quando sono disponibili "
        f"più mailbox. Valori accettati: {accounts}"
    )


# ── where a topic file comes from / goes to ────────────────────────────────

def _topic_of(a: dict) -> tuple[str | None, str | None]:
    """Topic indicato negli argomenti, altrimenti quello del canale corrente
    (dal claim firmato, letto dal gateway)."""
    tier, name = a.get("tier"), a.get("name")
    if tier and name:
        return tier, name
    return _host.get().current_topic()


def _topic_attachments(a: dict, agent: str) -> tuple[list, str | None, list]:
    """Materializza `topic_files` in una dir temporanea e ne ritorna i path,
    la dir (da cancellare nel `finally` del chiamante) e i byte letti.

    Il compartimento vale come per ogni altro accesso (`host.topics.read_file`,
    membership): un agente non allega file di topic di cui non è partecipante.
    """
    rels = [r for r in (a.get("topic_files") or []) if str(r).strip()]
    if not rels:
        return [], None, []
    tier, tname = _topic_of(a)
    if not (tier and tname):
        raise ValueError(
            "topic_files: non sono riuscito a ricavare il topic dal canale in cui "
            "stai lavorando — passa tier + name")
    topics = _host.get().topics
    d = tempfile.mkdtemp(prefix="attach-")
    out, blobs = [], []
    try:
        for rel in rels:
            # The crossing is noted by the caller, as `attachment`, after the
            # agent's own attachments: the native order of the audit record.
            data = topics.read_file(tier, tname, str(rel), crossing=None)
            dest = os.path.join(d, os.path.basename(str(rel)))
            with open(dest, "wb") as f:
                f.write(data)
            out.append(dest)
            blobs.append(data)
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    LOG.info("email: %d allegati per riferimento da %s/%s (agent %s)",
             len(out), tier, tname, agent)
    return out, d, blobs


def _agent_attachments(paths) -> list[str]:
    """Attachment paths passed by an agent, confined to its own scratch
    (clodia-platform#461): each must resolve (symlinks included) inside the
    caller's scratch and be a regular file; the resolved path is what gets
    sent."""
    host = _host.get()
    out: list[str] = []
    for raw in paths or []:
        try:
            rp = host.scratch_path(str(raw))
        except ValueError:
            raise ValueError(
                f"attachment outside your scratch: '{raw}'. Attach files from your "
                f"scratch, or topic files with topic_files=[...]") from None
        try:
            st = os.stat(rp)
        except OSError:
            raise ValueError(f"attachment not found: '{raw}'") from None
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"attachment is not a regular file: '{raw}'")
        out.append(rp)
    return out


# ── the verbs ──────────────────────────────────────────────────────────────

def _send(a: dict) -> Any:
    # ALLEGATI PER RIFERIMENTO (#104 §8): il gateway legge i file dal topic e
    # li allega, il contenuto non entra nel contesto dell'agente.
    host = _host.get()
    agent = host.agent() or ""
    own = _agent_attachments(a.get("attachments"))
    extra, tmpdir, blobs = _topic_attachments(a, agent)
    try:
        for p in own:
            host.note_crossing(path=p, kind="attachment")
        for data in blobs:
            host.note_crossing(data=data, kind="attachment")
        return mail.send(
            a["to"],
            a["subject"],
            a["body"],
            account=_email_account(a),
            cc=a.get("cc"),
            attachments=own + extra,
        )
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _folders(a: dict) -> Any:
    return mail.folders(account=_email_account(a))


def _list(a: dict) -> Any:
    return mail.list_messages(account=_email_account(a),
                              folder=a.get("folder", "INBOX"),
                              limit=a.get("limit", 10))


def _read(a: dict) -> Any:
    return mail.read_message(a["email_id"], account=_email_account(a),
                             folder=a.get("folder", "INBOX"))


def _get_attachment(a: dict) -> Any:
    return mail.get_attachment(a["email_id"], a["filename"],
                               account=_email_account(a),
                               folder=a.get("folder", "INBOX"))


def _save_attachment(a: dict) -> Any:
    # I byte dell'allegato NON passano dal modello: o nello scratch validato, o
    # DIRETTAMENTE nei file del topic (ricezione per riferimento).
    host = _host.get()
    dest_arg = (a.get("dest") or "").strip()
    # Senza `dest` si archivia nel topic: quello indicato, o QUELLO IN CUI SI
    # STA LAVORANDO.
    tier, tname = (None, None) if dest_arg else _topic_of(a)
    if not (dest_arg or (tier and tname)):
        raise ValueError(
            "non sono riuscito a ricavare il topic dal canale: passa "
            "`tier`+`name`, oppure `dest` per scrivere nel tuo scratch")
    raw, meta = mail.get_attachment_bytes(
        a["email_id"], a["filename"], account=_email_account(a),
        folder=a.get("folder", "INBOX"))
    if tier and tname:
        # Il nome dell'allegato è un NOME, non un path (clodia-platform#420).
        fn = mail.safe_attachment_name(a["filename"])
        # Provenienza `untrusted` d'ufficio, e il canale risulta contaminato:
        # `put_file` del host fa entrambe le cose, con il controllo di
        # compartimento.
        r = host.topics.put_file(tier, tname, fn, raw, provenance="untrusted")
        LOG.info("email: allegato '%s' archiviato in %s/%s (untrusted)",
                 fn, tier, tname)
        return {"topic_path": r["path"], "provenance": r["provenance"],
                "size": len(raw), "tainted": True, **meta}
    dest = host.scratch_path(dest_arg)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(raw)
    return {"local_path": dest, "size": len(raw), **meta}


def _search(a: dict) -> Any:
    return mail.search(a["query"], account=_email_account(a),
                       folder=a.get("folder", "INBOX"), limit=a.get("limit", 20))


def _reply(a: dict) -> Any:
    host = _host.get()
    own = _agent_attachments(a.get("attachments"))
    for p in own:
        host.note_crossing(path=p, kind="attachment")
    return mail.reply(a["email_id"], a["body"], account=_email_account(a),
                      folder=a.get("folder", "INBOX"), cc=a.get("cc"),
                      attachments=own)


HANDLERS: dict[str, Callable[[dict], Any]] = {
    "email.send": _send,
    "email.folders": _folders,
    "email.list": _list,
    "email.read": _read,
    "email.get_attachment": _get_attachment,
    "email.save_attachment": _save_attachment,
    "email.search": _search,
    "email.reply": _reply,
}


def handler_for(name: str) -> Callable[[dict], Any]:
    fn = HANDLERS[name]

    def handle(arguments: dict) -> Any:
        return fn(dict(arguments or {}))

    handle.__name__ = f"handle_{name.replace('.', '_')}"
    return handle


# ── the PDP's facts ────────────────────────────────────────────────────────

def _reply_recipient(arguments: dict) -> str:
    """Destinatario reale di un `email.reply`, letto dal messaggio originale.

    Stringa vuota se non risolvibile → il PDP la tratta come destinazione
    ignota e nega: «l'attaccante scrive, l'agente risponde con i dati» è il
    percorso dell'injection, e non sapere a chi si risponde non è una buona
    ragione per procedere."""
    try:
        msg = mail.read_message(
            str(arguments.get("email_id") or ""),
            account=_email_account(arguments),
            folder=arguments.get("folder") or "INBOX")
    except Exception as e:  # noqa: BLE001 — non risolvibile ≠ consentito
        LOG.warning("egress: destinatario di email.reply non risolvibile (%s)", e)
        return ""
    src = msg.get("from") or (msg.get("message") or {}).get("from") or ""
    return _host.get().perimeter.address_of(str(src))


def pdp_arguments(verb: str, arguments: dict) -> dict:
    """The facts the egress PDP needs and the call does not carry
    (`components.service("comms-pack", "pdp_arguments")`).

    `email.reply` has no recipient in its arguments: it is the SENDER of the
    message being answered, read here from the mailbox. The gateway keeps only
    the destination arguments the verb's schema does not offer (`to`, `bcc`)
    from this answer and drops the caller's value for them — an agent cannot
    pass `to` and have the reply judged by an address it chose."""
    if verb == "email.reply":
        return {"to": _reply_recipient(arguments)}
    return {}
