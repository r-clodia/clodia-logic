"""The mail code of the `email.*` verbs — accounts, mailbox whitelist, strict
ingress, the IMAP/SMTP client run.

Moved from clodia-tools `server/tools/email.py` (gateway 2.109.0) into
comms-pack's gateway component (clodia-platform#558). The code is the
gateway's, function by function, with the same names, the same decisions and
the same refusal texts; what changed is only HOW it reaches the gateway:

    gateway (`email.py`)                      here
    ─────────────────────────────────────     ──────────────────────────────────
    `vault.*` on `mailbox_`                   `_host.vault` → host.credentials("mailbox_")
    `vault.*` on `google_`/`gmail_`           google-pack's `mail_accounts` (address only)
    `vault.materialize_google_oauth` + run    google-pack's `mail_exec` (its own files)
    `egress.mailbox_allowed`                  host.perimeter.mailbox_allowed
    `egress.ingress_strict`/`_scope_of_call`  host.perimeter.strict_scope
    `egress.address_of`/`is_vetted_source`    host.perimeter.address_of / source_vetted
    `audit.policy.decision`                   host.audit_decision
    `whitelist.tool_allowed` / `agent_name`   host.tool_allowed / host.agent
    `secrets/email_config.json` (legacy)      host.perimeter.legacy_mail_accounts (names,
                                              addresses) — and refused: see below

The IMAP/SMTP client is the bundled `email_client.py` (the gateway's
`vendor/email_client.py`, verbatim), run in the gateway's process tree as
before, with ONE call's mailbox credential in a private 0700 directory and a
minimal environment (no gateway variable reaches it; no bytecode is written
next to the pinned component).

LEGACY accounts (`secrets/email_config.json`, no vault credential) are listed
— so that an agent learns they exist — and REFUSED at use, with the text the
pack position has given since #523: the gateway's whole secrets file is not one
mailbox's credential, and handing it to a pack's code is what the switch must
not do. They keep working on `mail: native`.
"""
from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Sequence, Union

from . import _host
from ._host import vault

LOG = logging.getLogger("clodia-tools.component.comms-pack.mail")

_EMAIL_PY = sys.executable
_EMAIL_SCRIPT = str(Path(__file__).resolve().parent / "email_client.py")

_VAULT_PREFIXES = ("google_", "gmail_", "mailbox_")
_REQUIRED_FIELDS = {
    "google": ("client_id", "client_secret", "refresh_token", "email"),
    "gmail": ("client_id", "client_secret", "refresh_token", "email"),
    # Il minimo di una casella è **spedire**. L'IMAP è opzionale, e non per
    # tolleranza: esistono indirizzi che sono alias con SMTP e nessuna casella
    # dietro (team@uncommon-digital.it). Richiedere l'IMAP li dichiarava «non
    # operativi» — un giudizio falso su una configurazione legittima, che poi
    # nascondeva l'account agli agenti come se fosse rotto.
    "mailbox": ("email", "smtp_server", "smtp_port"),
}

#: Frase con cui si riconosce il rifiuto di ingresso stretto senza leggerne il
#: testo: il gateway (`main._denial_class`) la cerca per classificare la
#: decisione come `sender_not_vetted` nel registro (#436). Identica a quella del
#: gateway (`server/tools/email.py`): un test lo verifica.
MITTENTE_NON_VAGLIATO = "mittente non vagliato"


# ── the gateway, through the host ──────────────────────────────────────────

def tool_allowed(verb: str) -> None:
    """The whitelist's own answer for `verb` (raises PermissionError)."""
    _host.get().tool_allowed(verb)


def agent_name() -> str:
    return _host.get().agent()


def _perimeter():
    return _host.get().perimeter


def is_send_only(bundle: dict) -> bool:
    """Casella di solo invio: SMTP dichiarato, nessun IMAP.

    È una **forma dichiarata**, non un errore ingoiato: se si assorbisse il
    fallimento IMAP, un guasto vero del server diventerebbe indistinguibile da
    una scelta di configurazione, e una lettura risponderebbe «nessun
    messaggio» — che non è «non posso leggere», è una bugia con la stessa forma
    di una verità.
    """
    return not (bundle.get("imap_server") or "").strip()


def _gmail_cred(account: str) -> str:
    # Preferisci la credenziale Google UNIFICATA (google_<account>, che include lo
    # scope Gmail); fallback al legacy gmail_<account>.
    if vault.has_credential(f"google_{account}"):
        return f"google_{account}"
    return f"gmail_{account}"


def _mailbox_cred(account: str) -> str:
    return f"mailbox_{account}"


# ── legacy accounts: listed, refused at use ────────────────────────────────

def _legacy() -> dict:
    try:
        return dict(_perimeter().legacy_mail_accounts() or {})
    except Exception:  # noqa: BLE001 — unreadable: no legacy account
        return {}


def _legacy_accounts() -> set[str]:
    return set(_legacy())


def _legacy_address(account: str) -> str | None:
    """Indirizzo di un account LEGACY, o `None` (letto dal gateway)."""
    addr = str(_legacy().get(account) or "").strip().lower()
    return addr or None


#: The refusal of a legacy account in `pack` position (since #523).
def _legacy_refusal(account: str) -> PermissionError:
    return PermissionError(
        f"l'account legacy '{account}' (email_config.json) non si usa con "
        "`mail: pack`: il componente di comms-pack riceve la credenziale di UNA "
        "casella dalla vault, e un account legacy non ne ha una sua. Spostalo "
        "nella vault (Integrazioni → Email) e riprova.")


# ── accounts ───────────────────────────────────────────────────────────────

def credential_diagnostics() -> list[dict]:
    """Stato materializzabile delle credenziali email, senza valori segreti.

    `mailbox_*`: calcolato qui, dal bundle (scope `mailbox_`). `google_*` /
    `gmail_*`: le righe di google-pack, che è chi ne custodisce il bundle —
    stessa forma, stessi campi richiesti."""
    google = {str(r["credential"]): r for r in _host.google_rows()}
    rows = []
    for credential in vault.store_names():
        prefix = next((p for p in _VAULT_PREFIXES if credential.startswith(p)), None)
        if prefix is None:
            continue
        kind = prefix[:-1]
        account = credential[len(prefix):]
        if kind != "mailbox":
            g = google.get(credential)
            if g is None:
                continue
            rows.append({k: g.get(k) for k in (
                "credential", "account", "kind", "operational", "send_only",
                "missing", "error")})
            continue
        missing: list[str] = []
        error = None
        solo_invio = False
        try:
            bundle = vault.read_internal(credential)
            missing = [field for field in _REQUIRED_FIELDS[kind] if not bundle.get(field)]
            if not (bundle.get("password") or bundle.get("app_password")):
                missing.append("password|app_password")
            solo_invio = is_send_only(bundle)
        except Exception as exc:  # noqa: BLE001 - diagnostica, mai valori
            error = type(exc).__name__
        rows.append({
            "credential": credential,
            "account": account,
            "kind": kind,
            "operational": not missing and error is None,
            # Dichiarato, non dedotto da un fallimento: chi legge questa riga
            # deve sapere che l'account NON si legge, prima di provarci.
            "send_only": solo_invio,
            "missing": missing,
            "error": error,
        })
    return rows


def known_accounts() -> set[str]:
    """Account email disponibili: Gmail OAuth (da google-pack), caselle
    generiche (mailbox_*) e i legacy da email_config.json."""
    return _legacy_accounts() | {
        row["account"] for row in credential_diagnostics() if row["operational"]
    }


def available_accounts(agent: str) -> list[str]:
    """Account operativi ESISTENTI (indipendente dall'agente). Il filtro reale —
    quale casella un canale può davvero leggere o scrivere — è la whitelist
    `inbox:`/`outbox:` per-scope, verificata al momento della chiamata
    (`_secrets_env`), non qui: qui si elenca cosa esiste, non cosa è permesso."""
    return sorted(known_accounts())


def _account_address(credential: str, account: str) -> str:
    try:
        bundle = vault.read_internal(credential)
    except Exception:  # noqa: BLE001 — diagnostica, non deve bloccare l'elenco
        return account
    return (bundle.get("email") or account).strip().lower()


def mailbox_address(account: str) -> str | None:
    """Indirizzo della casella `mailbox_<account>`, o `None` se non c'è.

    Serve a chi deve RITIRARE le autorizzazioni di una casella che sta per
    sparire (clodia-platform#411): l'indirizzo va letto PRIMA della rimozione.
    Non passa da `system_mailboxes` (solo operative) e non ripiega sul nome
    dell'account: un nome non è un indirizzo."""
    try:
        bundle = vault.read_internal(_mailbox_cred(account))
    except Exception:  # noqa: BLE001 — casella assente o illeggibile
        return None
    addr = (bundle.get("email") or "").strip().lower()
    return addr or None


def system_mailboxes() -> list[dict]:
    """Caselle di sistema selezionabili come connettore di un canale: gli
    account operativi con una credenziale, con il loro INDIRIZZO (è l'indirizzo
    che finisce nella whitelist `inbox:`/`outbox:`). I legacy restano fuori."""
    return sorted(
        (
            {
                "account": row["account"],
                "email": _account_address(row["credential"], row["account"]),
                "send_only": bool(row.get("send_only")),
            }
            for row in credential_diagnostics()
            if row["operational"]
        ),
        key=lambda r: r["account"],
    )


def accounts_not_allowed(direction: str, scope: str | None = None) -> list[str]:
    """Account che ESISTONO e funzionano, ma la cui casella non è nella
    whitelist `inbox:`/`outbox:` di questo canale (legacy inclusi, #503)."""
    per = _perimeter()
    out = []
    for row in credential_diagnostics():
        if not row["operational"]:
            continue
        addr = _account_address(row["credential"], row["account"])
        if not per.mailbox_allowed(direction, addr, scope):
            out.append(row["account"])
    for nome in _legacy_accounts():
        addr = _legacy_address(nome)
        # Senza indirizzo non è confrontabile e `_secrets_env` lo rifiuta:
        # dichiararlo non ammesso è il referto vero, non una cautela.
        if not addr or not per.mailbox_allowed(direction, addr, scope):
            out.append(nome)
    return sorted(set(out))


# ── strict ingress (#503) ──────────────────────────────────────────────────

def strict_scope() -> str | None:
    """Lo scope della chiamata SE è un canale a ingresso stretto, altrimenti
    `None` (fuori da un canale non c'è stretto)."""
    return _perimeter().strict_scope()


def _sender_vetted(sender: str, scope: str) -> bool:
    """Il mittente è dichiarato fidato per `scope`? Mittente non parsabile →
    `False`, fail-closed come `UNKNOWN` in uscita."""
    per = _perimeter()
    addr = per.address_of(sender or "")
    return bool(addr) and per.source_vetted(f"mailfrom:{addr}", scope)


def _record_ingress(verb: str, result: str) -> None:
    """La decisione di ingresso come RECORD: se il registro non si può
    scrivere, la lettura non si fa."""
    host = _host.get()
    try:
        host.audit_decision(verb, "ingress", result, reason_class="sender_not_vetted")
    except host.AuditUnavailable:
        raise PermissionError(
            "audit trail non disponibile: la decisione di ingresso non si può "
            "registrare, quindi la lettura non si fa"
        ) from None
    except Exception as e:  # noqa: BLE001 — la decisione vale anche se non registrata
        LOG.error("audit: decisione di ingresso di %s non registrata (%s)",
                  verb, type(e).__name__)


def _deny_sender(verb: str, sender: str) -> None:
    """Rifiuta la lettura di un messaggio di mittente non vagliato."""
    addr = _perimeter().address_of(sender or "") or "?"
    _record_ingress(verb, "deny")
    raise PermissionError(
        f"{MITTENTE_NON_VAGLIATO}: questo canale è a INGRESSO STRETTO e ammette "
        "solo i messaggi di chi è nella stanza (owner e partecipanti) o di un "
        f"mittente dichiarato. Per leggere questo messaggio serve mailfrom:{addr} "
        "negli ingress del canale, e lo aggiunge l'owner."
    )


def _filter_by_sender(rows: object, scope: str, verb: str) -> tuple[list, int]:
    """Tiene solo i messaggi di mittente vagliato; ritorna anche quanti ne ha
    tolti. Si filtra DOPO il client, mai con una query IMAP che scrive il
    chiamante; di ciò che si toglie esce solo il NUMERO."""
    if rows is None:
        return [], 0
    if not isinstance(rows, list):
        # Not a list (e.g. `{"raw": …}` when the client's output did not
        # parse): nothing here can be vetted, so nothing passes on a strict
        # channel (clodia-platform#560). Counted as one hidden result.
        _record_ingress(verb, "filter")
        return [], 1
    tenuti = [r for r in rows
              if isinstance(r, dict) and _sender_vetted(str(r.get("from") or ""), scope)]
    tolti = len(rows) - len(tenuti)
    if tolti:
        _record_ingress(verb, "filter")
    return tenuti, tolti


def _sender_of(account: str, email_id: str, folder: str, direction: str) -> str:
    """Il `From:` del messaggio `email_id`, letto apposta per vagliarlo (una
    fetch in più, solo nei canali stretti e solo per i tre verbi che non lo
    conoscono già). Il corpo resta qui: esce solo l'indirizzo."""
    msg = _run_cli(account, ["read", str(email_id), "--folder", folder],
                   want_json=True, direction=direction)
    return str((msg or {}).get("from") or "") if isinstance(msg, dict) else ""


def _assert_sender_vetted(verb: str, account: str, email_id: str, folder: str,
                          scope: str, *, direction: str = "inbox") -> None:
    """Rifiuta prima di restituire, se il mittente del messaggio non è vagliato.
    `direction` è quella del verbo chiamante (`reply` → `outbox`, #428)."""
    mittente = _sender_of(account, email_id, folder, direction)
    if not _sender_vetted(mittente, scope):
        _deny_sender(verb, mittente)


# ── the mailbox whitelist ──────────────────────────────────────────────────

def _assert_mailbox_allowed(direction: str, addr: str) -> None:
    """La casella `addr` è usabile in questa direzione da questo canale?"""
    if _perimeter().mailbox_allowed(direction, addr):
        return
    lista = "ingress" if direction == "inbox" else "egress"
    raise PermissionError(
        f"la casella '{addr}' non è nella whitelist {direction} di questo "
        f"canale. Chiedi all'owner di aggiungere {direction}:{addr} agli "
        f"{lista} del topic (o globalmente, da Integrazioni)."
    )


#: Prefix of the per-call secrets directory.
_SECRETS_TMP_PREFIX = "email_sec_"


def _client_env(secrets_dir: str) -> dict:
    """The environment of ONE client run: nothing of the gateway's own (its
    variables are not the client's business), the call's secrets directory,
    and no bytecode next to the pinned component."""
    return {
        "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
        "LANG": os.environ.get("LANG") or "C.UTF-8",
        "HOME": secrets_dir,
        "CLODIA_SECRETS_DIR": secrets_dir,
        "PYTHONDONTWRITEBYTECODE": "1",
    }


@contextlib.contextmanager
def _secrets_env(account: str, direction: str):
    """La whitelist della casella per `direction`, poi l'ambiente del client.

    - Gmail (`google_`/`gmail_`, di google-pack): si controlla l'INDIRIZZO
      (letto da google-pack) e si cede `{}` — i file XOAUTH2 li prepara
      google-pack per la sua chiamata (`mail_exec`), non passano di qui;
    - casella (`mailbox_<account>`): `email_config.json` con il SOLO bundle di
      questa casella in un dir effimero 0700, cancellato a fine chiamata;
    - legacy: stessa whitelist per indirizzo (#503), poi rifiutato.

    `direction` ("inbox" per leggere, "outbox" per inviare/rispondere) decide
    QUALE whitelist si consulta.
    """
    gcred, mcred = _gmail_cred(account), _mailbox_cred(account)
    cred = gcred if vault.has_credential(gcred) else (
        mcred if vault.has_credential(mcred) else None)
    if cred == mcred and _host.google_mail_account_exists(account):
        # A Google account whose google-pack is not serving: never fall back
        # to a same-named IMAP mailbox — another sender address, and a reply
        # resolved in one mailbox sent from the other (review of #384).
        raise PermissionError(
            f"l'account '{account}' è un account Google: google-pack non è "
            "attivo su questa istanza, quindi la sua posta non si usa ora")
    if cred is None:
        addr = _legacy_address(account)
        if not addr:
            raise PermissionError(
                f"l'account legacy '{account}' non dichiara un indirizzo in "
                "email_config.json: senza indirizzo non è confrontabile con la "
                f"whitelist {direction} di questo canale e non si usa. Aggiungi "
                "'email' alla sua voce, o spostalo nella vault."
            )
        _assert_mailbox_allowed(direction, addr)
        raise _legacy_refusal(account)
    bundle = vault.read_internal(cred)
    addr = (bundle.get("email") or account).strip().lower()
    _assert_mailbox_allowed(direction, addr)
    if cred == gcred:
        yield {}
        return
    tmp = tempfile.mkdtemp(prefix=_SECRETS_TMP_PREFIX)
    try:
        os.chmod(tmp, 0o700)
        cfg = {"default": account, "accounts": {account: bundle}}
        cfg_path = Path(tmp) / "email_config.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        os.chmod(cfg_path, 0o600)
        yield _client_env(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _is_gmail(account: str) -> bool:
    """The account runs on a Google credential (google-pack's) — the same test
    `_secrets_env` uses to pick the credential, so routing and files agree."""
    return vault.has_credential(_gmail_cred(account))


def _unknown_account(account: str) -> ValueError:
    why = _host.google_unavailable()
    extra = f" ({why})" if why else ""
    return ValueError(
        f"unknown account '{account}'; available: {sorted(known_accounts())}{extra}"
    )


def _gmail_runner(account: str):
    """google-pack's `mail_exec`, or a refusal: never another client."""
    svc = _host.google_service(_host.GMAIL_EXEC)
    if svc is None:
        raise PermissionError(
            f"Gmail provider not available for '{account}': "
            f"{_host.google_unavailable() or 'google-pack has no mail_exec service'}")
    return svc


class _Result:
    """The shape `_run_cli` reads from `subprocess.run`, from google-pack."""

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _gmail_payload(account: str, cli_args: list[str], timeout: int) -> dict:
    """`{account, argv, files, timeout}` for google-pack's `mail_exec`.

    Only the call and its ATTACHMENTS: each `--attachment` travels as bytes
    (`att/<n>/<name>`, one directory per file so the MIME part keeps its
    name) and the argument becomes `{dir}/att/<n>/<name>`. No credential: the
    XOAUTH2 files are google-pack's to write."""
    files: dict[str, str] = {}
    argv: list[str] = []
    n = 0
    it = iter(cli_args)
    for arg in it:
        if arg != "--attachment":
            argv.append(arg)
            continue
        raw = next(it, None)
        if raw is None:
            raise ValueError("--attachment senza percorso")
        src = Path(raw)
        rel = f"att/{n}/{src.name}"
        files[rel] = base64.b64encode(src.read_bytes()).decode("ascii")
        argv += ["--attachment", "{dir}/" + rel]
        n += 1
    return {"account": account, "argv": argv, "files": files, "timeout": timeout}


def _run_gmail(runner, account: str, cli_args: list[str], timeout: int) -> _Result:
    try:
        data = runner(_gmail_payload(account, cli_args, timeout))
    except Exception as e:  # noqa: BLE001 — the provider failed, the verb did
        raise RuntimeError(
            f"email {cli_args[0]} failed: mail provider 'google-pack.mail_exec' "
            f"error: {e}") from None
    try:
        rc = data["returncode"]
        if not isinstance(rc, int) or isinstance(rc, bool):
            raise TypeError("returncode")
        return _Result(rc, str(data.get("stdout") or ""), str(data.get("stderr") or ""))
    except (KeyError, TypeError):
        raise RuntimeError(
            f"email {cli_args[0]} failed: mail provider 'google-pack.mail_exec' "
            "answered without {returncode, stdout, stderr}") from None


def _run_cli(account: str, cli_args: list[str], *, want_json: bool,
             direction: str, timeout: int = 60) -> Union[dict, list]:
    """Esegue il client per `account`. Ritorna il JSON parsato (letture) o un
    dict di esito (send/reply).

    `direction`: "inbox" per i verbi di lettura, "outbox" per invio/risposta —
    quale whitelist per-casella verificare (vedi `_secrets_env`). Un account
    Google va a google-pack (`mail_exec`), una casella al client incluso; un
    legacy è rifiutato. Mai un ripiego sul client nativo del gateway."""
    if account not in known_accounts():
        raise _unknown_account(account)
    if _is_gmail(account):
        runner = _gmail_runner(account)
        with _secrets_env(account, direction):
            result = _run_gmail(runner, account, cli_args, timeout)
    else:
        # A legacy account (no vault credential) is refused inside
        # `_secrets_env`, after the same address check as natively (#503).
        with _secrets_env(account, direction) as env:
            cmd = [_EMAIL_PY, _EMAIL_SCRIPT, "--account", account, *cli_args]
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=timeout, env=env)
    if result.returncode != 0:
        raise RuntimeError(
            f"email {cli_args[0]} failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    out = result.stdout.strip()
    if not want_json:
        return {"stdout": out}
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out}


def _run_json(account: str, cli_args: list[str], *, timeout: int = 60,
              direction: str = "inbox") -> Union[dict, list]:
    """Un comando di lettura/risposta che ritorna JSON. Un punto solo per la
    guardia «questa casella si legge?»: qui passano tutti i verbi che leggono, e
    **solo** `send` non passa da qui. `reply` passa con `direction="outbox"`."""
    if direction == "inbox":
        _assert_readable(account)
    return _run_cli(account, cli_args, want_json=True, direction=direction, timeout=timeout)


def _assert_readable(account: str) -> None:
    """Rifiuta una LETTURA su una casella di solo invio, dicendo perché (una
    lista vuota avrebbe la stessa forma di una verità)."""
    cred = _mailbox_cred(account)
    if not vault.has_credential(cred):
        return                      # google/legacy: la lettura è normale
    try:
        bundle = vault.read_internal(cred)
    except Exception:  # noqa: BLE001 — la diagnostica non deve bloccare l'accesso
        return
    if is_send_only(bundle):
        raise PermissionError(
            f"l'account '{account}' è di SOLO INVIO: è un alias con SMTP e nessuna "
            "casella IMAP dietro, quindi non c'è niente da leggere — non è un "
            "guasto e riprovare non cambia nulla. Per avere traccia di ciò che "
            f"spedisci da '{account}', mettiti in CC un account leggibile."
        )


def _attachment_args(attachments: Optional[Sequence[str]]) -> list[str]:
    """Converte path locali in flag --attachment, validandoli presto."""
    args: list[str] = []
    for raw in attachments or []:
        path = Path(str(raw)).expanduser()
        if not path.is_file():
            raise ValueError(f"attachment not found or not a file: '{raw}'")
        args += ["--attachment", str(path)]
    return args


# ── the verbs ──────────────────────────────────────────────────────────────

def folders(account: str = "demo") -> dict:
    """Elenca le cartelle IMAP dell'account."""
    tool_allowed("email.folders")
    ag = agent_name()
    out = {
        "account": account,
        "available_accounts": available_accounts(ag),
        "folders": _run_json(account, ["folders"]),
    }
    # Quali fra quelle disponibili servono SOLO a spedire, detto nell'elenco.
    solo_invio = [r["account"] for r in credential_diagnostics()
                  if r.get("send_only") and r["account"] in out["available_accounts"]]
    if solo_invio:
        out["send_only_accounts"] = solo_invio
    # Se esistono caselle che questo canale non può leggere, lo si DICE.
    negati = accounts_not_allowed("inbox")
    if negati:
        out["accounts_not_allowed"] = negati
        out["note"] = (
            f"esistono e funzionano anche: {', '.join(negati)} — ma la loro "
            "casella non è nella whitelist di questo canale. Chiedi all'owner "
            "di aggiungere inbox:<email> agli ingress del topic (o "
            "globalmente)."
        )
    return out


def list_messages(account: str = "demo", folder: str = "INBOX", limit: int = 10) -> dict:
    """Elenca i messaggi di una cartella (default INBOX)."""
    tool_allowed("email.list")
    out = {
        "account": account,
        "folder": folder,
        "messages": _run_json(account, ["list", "--folder", folder, "--limit", str(limit)]),
    }
    out["messages"], nascosti = _hide_box_mail(out["messages"], account)
    if nascosti:
        out["message_box_withheld"] = nascosti
    scope = strict_scope()
    if scope:
        out["messages"], tolti = _filter_by_sender(out["messages"], scope, "email.list")
        out["strict_ingress"] = True
        if tolti:
            out["withheld"] = tolti
            out["note"] = (
                f"{tolti} messaggi non mostrati: canale a ingresso stretto, il "
                "mittente non è nella stanza né dichiarato fra gli ingress. "
                "Di questi messaggi non si vede nulla, nemmeno l'oggetto."
            )
    return out


def read_message(email_id: str, account: str = "demo", folder: str = "INBOX") -> dict:
    """Legge un singolo messaggio per ID."""
    tool_allowed("email.read")
    out = _run_json(account, ["read", str(email_id), "--folder", folder])
    if _is_box_mail(out, account):
        _deny_box_mail("email.read")
    scope = strict_scope()
    # Sul RISULTATO, prima di restituirlo: il corpo non ha ancora lasciato il
    # gateway.
    if scope and not _sender_vetted(str((out or {}).get("from") or ""), scope):
        _deny_sender("email.read", str((out or {}).get("from") or ""))
    return out


def safe_attachment_name(filename: str, fallback: str = "allegato") -> str:
    """Il nome di un allegato ridotto a UN nome di file (clodia-platform#420):
    il nome lo sceglie il mittente, e un `/` non deve diventare una cartella."""
    name = re.sub(r"[\\/]+", "-", filename or "")
    name = re.sub(r"\s+", " ", name).strip(" .-")
    return name or fallback


def get_attachment(email_id: str, filename: str, account: str = "demo",
                   folder: str = "INBOX") -> dict:
    """Contenuto base64 di un allegato (per i binari grandi: save_attachment)."""
    tool_allowed("email.get_attachment")
    if not filename:
        raise ValueError("'filename' must be provided")
    scope = strict_scope()
    if scope:
        _assert_sender_vetted("email.get_attachment", account, email_id, folder, scope)
    r = _run_json(account, ["get-attachment", str(email_id), "--filename", filename,
                            "--folder", folder])
    if _is_box_mail(r, account):
        _deny_box_mail("email.get_attachment")
    return r


def get_attachment_bytes(email_id: str, filename: str, account: str = "demo",
                         folder: str = "INBOX") -> tuple[bytes, dict]:
    """Byte DECODIFICATI di un allegato + metadati — nessun base64 verso il
    modello. Backend di email.save_attachment."""
    tool_allowed("email.save_attachment")
    if not filename:
        raise ValueError("'filename' must be provided")
    scope = strict_scope()
    if scope:
        _assert_sender_vetted("email.save_attachment", account, email_id, folder, scope)
    r = _run_json(account, ["get-attachment", str(email_id), "--filename", filename,
                            "--folder", folder])
    if not isinstance(r, dict) or not r.get("data"):
        raise ValueError(f"allegato '{filename}' non trovato nel messaggio {email_id}")
    if _is_box_mail(r, account):
        _deny_box_mail("email.save_attachment")
    raw = base64.b64decode(r["data"])
    return raw, {"filename": r.get("filename") or filename,
                 "content_type": r.get("content_type")}


# ── the courier (#531): envelopes and attachments, never the body ─────────

def envelope_key(row: dict) -> str:
    """A stable key for one message: its Message-ID when it has one, else its
    date|from|subject. IMAP sequence ids shift when a message is deleted; the
    courier remembers what it delivered by this."""
    import hashlib
    mid = str(row.get("message_id") or "").strip()
    raw = f"mid|{mid}" if mid else "|".join(
        str(row.get(k) or "") for k in ("date", "from", "subject"))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]


def _courier_sender_ok(sender: str) -> bool:
    """Does the CURRENT room itself declare this sender (`mailfrom:` among its
    own ingress entries, #576)? What the courier delivers there: the same
    mailbox feeds many rooms, so global entries and participants do not count,
    and a room that declares nobody gets nothing. Needs clodia-tools ≥ 2.126.0;
    an older gateway delivers nothing rather than everything."""
    ask = getattr(_perimeter(), "room_declares_sender", None)
    if ask is None:
        raise RuntimeError("the mail courier needs clodia-tools ≥ 2.126.0 "
                           "(perimeter.room_declares_sender, clodia-platform#576)")
    return bool(ask(sender or ""))


def _courier_tag() -> str:
    """The message-box code of this courier call (#583), "" outside one. Set
    by the gateway's courier route, never by a caller; an older gateway → ""."""
    ask = getattr(_perimeter(), "courier_tag", None)
    return str(ask() or "") if ask else ""


def _box_address(account: str, tag: str) -> str:
    """`local+TAG@domain` of `account`'s mailbox, lower-case."""
    addr = _mailbox_addr(account)
    local, at, domain = addr.partition("@")
    return f"{local}+{tag.lower()}@{domain}" if (at and tag) else ""


def _mailbox_addr(account: str) -> str:
    """The address of `account`'s mailbox, lower-case, "" if unknown."""
    addr = str(mailbox_address(account) or "").strip().lower()
    if not addr:
        # A Google account (the colony mailbox may be one): its address comes
        # from the same list the gateway names the colony mailbox from.
        try:
            for row in system_mailboxes() or []:
                if str(row.get("account") or "") == account:
                    addr = str(row.get("email") or "").strip().lower()
                    break
        except Exception:  # noqa: BLE001 — no address, no delivery
            addr = ""
    return addr


def _tag_ok(row: object, account: str) -> bool:
    """Outside a message-box call: always. Inside: only a mail whose recipient
    headers AS SENT (`rcpt_raw`) name `box+TAG@…` — no fallback, a client
    without them delivers nothing (#583)."""
    tag = _courier_tag()
    if not tag:
        return True
    want = _box_address(account, tag)
    if not want or not isinstance(row, dict):
        return False
    from email.utils import getaddresses
    seen = {a.strip().lower() for _n, a in getaddresses(
        [str(x) for x in (row.get("rcpt_raw") or [])]) if a}
    return want in seen


def _is_box_mail(row: object, account: str) -> bool:
    """Is this a message-box mail (#583): on the COLONY mailbox and addressed to
    one of its plus sub-addresses (`local+X@domain`)? Such mail belongs to the
    topic whose code is X and to nobody else — not to a room that reads the
    colony mailbox through its own `inbox:`, not to an agent's `email.*`
    (review of clodia-tools#416). An older gateway (no `colony_address`) →
    never: there is no message box to protect."""
    ask = getattr(_perimeter(), "colony_address", None)
    colony = str(ask() or "").strip().lower() if ask else ""
    if not colony or not isinstance(row, dict):
        return False
    local, at, domain = colony.partition("@")
    if not account or _mailbox_addr(account) != colony:
        return False
    from email.utils import getaddresses
    for _n, a in getaddresses([str(x) for x in (row.get("rcpt_raw") or [])]):
        a = (a or "").strip().lower()
        if a.endswith("@" + domain) and a.startswith(local + "+"):
            return True
    return False


def _hide_box_mail(rows: object, account: str) -> tuple[object, int]:
    """Rows without the message-box mail, and how many were hidden."""
    if not isinstance(rows, list):
        return rows, 0
    tenuti = [r for r in rows if not _is_box_mail(r, account)]
    return tenuti, len(rows) - len(tenuti)


def _deny_box_mail(verb: str) -> None:
    _record_ingress(verb, "deny")
    raise PermissionError(
        "questo messaggio è indirizzato alla message box di un topic: lo riceve "
        "solo quel topic (clodia-platform#583)")


def _courier_rows(rows: object) -> tuple[list, int]:
    """The listed messages whose sender the room declares, and how many were
    left out (counted, recorded, never shown)."""
    if not isinstance(rows, list):
        _record_ingress("courier.mail", "filter")
        return [], 1 if rows else 0
    tenuti = [r for r in rows if isinstance(r, dict) and _courier_sender_ok(_raw_from(r))]
    tolti = len(rows) - len(tenuti)
    if tolti:
        _record_ingress("courier.mail", "filter")
    return tenuti, tolti


def _raw_from(row: dict) -> str:
    """The `From:` header as SENT (`from_raw`): the decoded one can turn an
    encoded display name into an address (review of clodia-tools#411). A
    client that does not give it → "" → the sender is not declared."""
    return str(row.get("from_raw") or "") if isinstance(row, dict) else ""


def _deny_courier(header: str) -> None:
    addr = _perimeter().address_of(header) or "?"
    _record_ingress("courier.mail", "deny")
    raise PermissionError(
        f"{MITTENTE_NON_VAGLIATO}: il corriere consegna in questa stanza solo "
        f"la posta dei mittenti che la stanza dichiara (mailfrom:{addr} fra i "
        "suoi ingress, lo aggiunge l'owner)")


def _assert_courier_sender(result: object) -> None:
    """Refuse a fetched message whose sender the room does not declare (#576),
    checked on the `From:` of what was ACTUALLY fetched: between two IMAP
    sessions a sequence id can name another message (review of #553)."""
    raw = _raw_from(result)
    if not _courier_sender_ok(raw):
        _deny_courier(raw)


def courier_envelopes(account: str, folder: str = "INBOX", limit: int = 20,
                      skip: list | None = None, keys_only: bool = False) -> dict:
    """The newest messages of a mailbox as ENVELOPES — id, key, from, to,
    subject, date, Message-ID, attachment names — for the gateway's courier
    (clodia-platform#531). Never the body: a message is read here only to
    learn its attachments, and its text is dropped before returning.

    Runs inside the ROOM's claims (the gateway's courier route sets them): the
    mailbox must be in the room's `inbox:` list (`_secrets_env`), and only the
    senders the room ITSELF declares pass (#576; strict or not), the others
    counted. `skip` lists keys the courier already delivered: those are not
    read again."""
    known = {str(k) for k in (skip or [])}
    limit = max(1, min(int(limit), 50))
    tag = _courier_tag()
    if tag:
        # The message box (#583): searched by recipient, not taken from the
        # newest N of the whole colony mailbox — busy between two passes, a
        # tagged mail would fall out of that window (review of #416).
        box = _box_address(account, tag)
        if not box:
            return {"account": account, "folder": folder, "withheld": 0,
                    **({"keys": []} if keys_only else {"envelopes": []})}
        query = f'OR OR TO "{box}" CC "{box}" HEADER Delivered-To "{box}"'
        rows = _run_json(account, ["search", query, "--folder", folder,
                                   "--limit", str(limit)])
    else:
        rows = _run_json(account, ["list", "--folder", folder, "--limit", str(limit)])
    rows, tolti = _courier_rows(rows)
    if not _courier_tag():
        rows, nascosti = _hide_box_mail(rows, account)
        tolti += nascosti
    if _courier_tag():
        # The colony mailbox (#583): only the mail addressed to this topic's
        # message box, counted apart from the senders' filter.
        tenuti = [r for r in rows if _tag_ok(r, account)]
        tolti += len(rows) - len(tenuti)
        rows = tenuti
    if keys_only:
        # The courier's first pass over a mailbox only records what is there:
        # the keys come from the listing, no message is read.
        return {"account": account, "folder": folder, "withheld": tolti,
                "keys": [envelope_key(r) for r in (rows if isinstance(rows, list) else [])
                         if isinstance(r, dict)]}
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        key = envelope_key(row)
        if key in known:
            continue
        full = _run_json(account, ["read", str(row.get("id")), "--folder", folder])
        if not isinstance(full, dict):
            continue
        # IMAP ids are sequence numbers: between the listing and this read the
        # same id can name ANOTHER message (an expunge in between). Only the
        # message that was listed — same key — goes out, and its sender is
        # checked again on what was actually read.
        if envelope_key(full) != key:
            continue
        if not _courier_sender_ok(_raw_from(full)) or not _tag_ok(full, account) \
                or (not _courier_tag() and _is_box_mail(full, account)):
            _record_ingress("courier.mail", "filter")
            tolti += 1
            continue
        out.append({"id": str(row.get("id")), "key": key,
                    "from": full.get("from"), "to": full.get("to"),
                    "subject": full.get("subject"), "date": full.get("date"),
                    "message_id": full.get("message_id"),
                    "attachments": [str(a) for a in (full.get("attachments") or [])]})
    return {"account": account, "folder": folder, "envelopes": out,
            "withheld": tolti}


def courier_attachment(account: str, email_id: str, filename: str,
                       folder: str = "INBOX") -> dict:
    """The decoded bytes (base64 for transport) of one attachment, for the
    courier to store in the topic's files byte-identical (#531). Same room
    rules as `courier_envelopes`: mailbox in the room's `inbox:` list, and a
    sender the room itself declares (#576)."""
    if not filename:
        raise ValueError("'filename' must be provided")
    r = _run_json(account, ["get-attachment", str(email_id), "--filename", filename,
                            "--folder", folder])
    if not isinstance(r, dict) or not r.get("data"):
        raise ValueError(f"allegato '{filename}' non trovato nel messaggio {email_id}")
    _assert_courier_sender(r)
    if not _tag_ok(r, account):
        _deny_courier(_raw_from(r))
    if not _courier_tag() and _is_box_mail(r, account):
        _deny_box_mail("courier.mail")
    raw = base64.b64decode(r["data"])
    return {"filename": safe_attachment_name(r.get("filename") or filename),
            "content_type": r.get("content_type"), "size": len(raw),
            "content_b64": base64.b64encode(raw).decode("ascii")}


def courier_eml(account: str, email_id: str, folder: str = "INBOX") -> dict:
    """The whole message as RFC822 bytes (base64 for transport), for the
    courier to keep a `.eml` copy in the topic (#576). Same room rules as
    `courier_attachment`. The bytes go to the topic's files, untrusted, never
    to a model."""
    if not email_id:
        raise ValueError("'email_id' must be provided")
    r = _run_json(account, ["get-eml", str(email_id), "--folder", folder])
    if not isinstance(r, dict) or not r.get("data"):
        raise ValueError(str((r or {}).get("error") or f"messaggio {email_id} non trovato")
                         if isinstance(r, dict) else f"messaggio {email_id} non trovato")
    _assert_courier_sender(r)
    if not _tag_ok(r, account):
        _deny_courier(_raw_from(r))
    if not _courier_tag() and _is_box_mail(r, account):
        _deny_box_mail("courier.mail")
    raw = base64.b64decode(r["data"])
    return {"size": len(raw), "message_id": r.get("message_id"),
            "content_b64": base64.b64encode(raw).decode("ascii")}


def search(query: str, account: str = "demo", folder: str = "INBOX", limit: int = 20) -> dict:
    """Cerca messaggi via query IMAP (es. FROM \"x@y.it\")."""
    tool_allowed("email.search")
    if not query:
        raise ValueError("'query' must be non-empty")
    out = {
        "account": account,
        "query": query,
        "results": _run_json(account, ["search", query, "--folder", folder, "--limit", str(limit)]),
    }
    out["results"], nascosti = _hide_box_mail(out["results"], account)
    if nascosti:
        out["message_box_withheld"] = nascosti
    scope = strict_scope()
    if scope:
        out["results"], tolti = _filter_by_sender(out["results"], scope, "email.search")
        out["strict_ingress"] = True
        if tolti:
            out["withheld"] = tolti
            out["note"] = (
                f"{tolti} risultati non mostrati: canale a ingresso stretto, il "
                "mittente non è nella stanza né dichiarato fra gli ingress."
            )
    return out


def reply(email_id: str, body: str, account: str = "demo",
          folder: str = "INBOX", cc: Optional[str] = None,
          attachments: Optional[Sequence[str]] = None) -> dict:
    """Risponde a un messaggio mantenendo il threading (SMTP)."""
    tool_allowed("email.reply")
    if body is None:
        raise ValueError("'body' must be provided (use empty string if intentional)")
    # Il destinatario di una risposta è il mittente del messaggio originale:
    # in un canale stretto rispondere a un mittente non vagliato è chiuso.
    scope = strict_scope()
    if scope:
        _assert_sender_vetted("email.reply", account, email_id, folder, scope,
                              direction="outbox")
    # Only on the colony mailbox (#583): elsewhere no extra read — it would ask
    # the `inbox` permission of a mailbox the room may hold for `outbox` only.
    ask = getattr(_perimeter(), "colony_address", None)
    colony = str(ask() or "").strip().lower() if ask else ""
    if colony and _mailbox_addr(account) == colony and _is_box_mail(
            _run_json(account, ["read", str(email_id), "--folder", folder]), account):
        _deny_box_mail("email.reply")
    args = ["reply", str(email_id), "--body", body, "--folder", folder]
    if cc:
        args += ["--cc", cc]
    args += _attachment_args(attachments)
    # `outbox`: la risposta la SPEDISCE questa casella (clodia-platform#428).
    return _run_json(account, args, direction="outbox")


def send(
    to: str,
    subject: str,
    body: str,
    account: str = "demo",
    cc: Optional[str] = None,
    attachments: Optional[Sequence[str]] = None,
) -> dict:
    """Invia una email via account configurato (allegati: path già validati)."""
    tool_allowed("email.send")
    if not to or "@" not in to:
        raise ValueError(f"invalid 'to' address: '{to}'")
    if not subject:
        raise ValueError("'subject' must be non-empty")
    if body is None:
        raise ValueError("'body' must be provided (use empty string if intentional)")

    args = ["send", "--to", to, "--subject", subject, "--body", body]
    if cc:
        args += ["--cc", cc]
    args += _attachment_args(attachments)
    # `outbox`: si controlla la casella che spedisce (clodia-platform#428).
    res = _run_cli(account, args, want_json=False, direction="outbox")
    return {
        "ok": True,
        "to": to,
        "subject": subject,
        "account": account,
        "attachments": [str(Path(str(p)).expanduser()) for p in attachments or []],
        "stdout": res.get("stdout", ""),
    }
