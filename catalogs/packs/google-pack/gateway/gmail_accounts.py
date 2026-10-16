"""Gmail for comms-pack's `email.*` verbs — the services `mail_accounts` and
`mail_exec` (clodia-platform#558, google-pack 2.1.0).

With `connectors: {mail: pack}` the eight `email.*` verbs are comms-pack's
gateway component's, for EVERY account: comms-pack owns the mail vocabulary.
A Google account (`google_<acct>` / `gmail_<acct>`) is still this pack's: its
credential is in this component's scope (`builtin.CORE_CREDENTIALS_FOLLOW`,
the `google` switch) and never in comms-pack's. So comms-pack asks here, and
only for two things, through `host.service("google-pack", …)` — a lookup the
gateway grants to comms-pack for exactly these two names
(`builtin.COMPONENT_SERVICES`):

    mail_accounts() -> [{credential, account, kind, operational, send_only,
                         missing, error, email}]
        the Google mail accounts, in the shape of the gateway's
        `email.credential_diagnostics()` rows plus the bundle's `email`.
        Names and addresses: never a token, a secret or a client id.

    mail_exec({account, argv, files, timeout}) -> {returncode, stdout, stderr}
        one run of the bundled client for `account`. comms-pack has already
        taken every decision (account, mailbox whitelist on the address
        above, strict ingress); `files` carries only that call's attachments
        (`att/…`). The XOAUTH2 files are written HERE, from this component's
        own credential, exactly as the gateway's
        `vault.materialize_google_oauth` writes them, and handed to
        `gmail_core.run` — the same per-call directory, scrubbing and clean-up
        as the `mail_run` service.

On `mail: native` the gateway's `email.py` keeps calling `mail_run` with the
files its vault materialised: these two services are unused there.
"""
from __future__ import annotations

import base64
import json

from . import _host
from . import gmail_core

#: The fields a Google bundle needs for Gmail (`email.py`'s `_REQUIRED_FIELDS`).
_REQUIRED = ("client_id", "client_secret", "refresh_token", "email")
#: `vault._GMAIL_SERVERS` of the gateway, verbatim.
_GMAIL_SERVERS = {
    "imap_server": "imap.gmail.com", "imap_port": 993,
    "smtp_server": "smtp.gmail.com", "smtp_port": 587,
}
_KINDS = (("google_", "google"), ("gmail_", "gmail"))


def mail_accounts() -> list[dict]:
    """The Google mail accounts as diagnostics rows plus their address."""
    rows = []
    for prefix, kind in _KINDS:
        for credential in _host.names(prefix):
            account = credential[len(prefix):]
            missing: list[str] = []
            error = None
            email = ""
            try:
                bundle = _host.read_internal(credential)
                missing = [f for f in _REQUIRED if not bundle.get(f)]
                email = str(bundle.get("email") or "")
            except Exception as exc:  # noqa: BLE001 — diagnostics, never values
                error = type(exc).__name__
            rows.append({
                "credential": credential,
                "account": account,
                "kind": kind,
                "operational": not missing and error is None,
                "send_only": False,
                "missing": missing,
                "error": error,
                "email": email,
            })
    return rows


def _credential(account: str) -> str:
    """`google_<account>` if it exists, else `gmail_<account>` (the gateway's
    `email._gmail_cred` preference)."""
    if _host.has_credential(f"google_{account}"):
        return f"google_{account}"
    return f"gmail_{account}"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _oauth_files(credential: str) -> dict[str, str]:
    """`secrets/…` files of ONE Google account, as the gateway's
    `vault.materialize_google_oauth` writes them (same JSON, byte for byte)."""
    b = _host.read_internal(credential)
    missing = [k for k in _REQUIRED if not b.get(k)]
    if missing:
        raise RuntimeError(f"vault: bundle '{credential}' incompleto, manca {missing}")
    account = b.get("account") or b["email"].split("@")[0].replace(".", "_")
    return {
        "secrets/google_oauth_client.json": _b64(json.dumps(
            {"client_id": b["client_id"], "client_secret": b["client_secret"]})),
        "secrets/email_oauth_tokens.json": _b64(json.dumps(
            {account: {"refresh_token": b["refresh_token"], "email": b["email"]}})),
        "secrets/email_config.json": _b64(json.dumps({
            "accounts": {account: {"email": b["email"], "auth": "oauth2",
                                   **_GMAIL_SERVERS}},
            "default": account,
        })),
    }


def mail_exec(payload: dict) -> dict:
    """One Gmail client run for `payload["account"]` (see the module doc)."""
    payload = payload or {}
    account = payload.get("account")
    if not isinstance(account, str) or not account or "\x00" in account:
        raise gmail_core.TransportError("account must be a non-empty string")
    files = payload.get("files") or {}
    if not isinstance(files, dict):
        raise gmail_core.TransportError("files must be an object {relpath: base64}")
    for rel in files:
        # The caller hands attachments only: a credential is never its to give.
        if not isinstance(rel, str) or not rel.startswith("att/"):
            raise gmail_core.TransportError(
                f"only attachments (att/…) may be passed, not {rel!r}")
    argv = payload.get("argv")
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise gmail_core.TransportError("argv must be a list of strings")
    credential = _credential(account)
    if not _host.has_credential(credential):
        raise PermissionError(f"'{account}' is not a Google mail account")
    return gmail_core.run({
        "argv": ["--account", account, *argv],
        "files": {**files, **_oauth_files(credential)},
        "timeout": payload.get("timeout"),
    })
