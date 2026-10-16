"""The host this component was registered with, and what the mail code asks of it.

`register(host)` (mail_component.py) stores the host here; every other module
of the component reaches the gateway ONLY through it — the gateway's import
guard refuses `server.*` imports: credentials come through
`host.credentials("mailbox_")`, the perimeter (mailbox whitelist, strict
ingress, vetted senders) through `host.perimeter`, the call's facts through
`host.agent()` / `host.current_topic()`, the record of an ingress decision
through `host.audit_decision`.

Credential scope. The component declares ONE entry (`pack.yaml`,
`components[0].credentials`): the prefix `mailbox_`. It stays the gateway's
custody (`builtin.CORE_CREDENTIALS_FOLLOW`): reachable only while the instance
switch is `connectors: {mail: pack}`, checked again at every access, so
turning the switch back revokes it at once. Nothing is copied.

Gmail. A Google account (`google_<acct>` / `gmail_<acct>`) is google-pack's:
its credential is NOT in this component's scope and never passes through it.
For the `email.*` verbs of such an account this component asks google-pack's
gateway component, through `host.service("google-pack", …)` — a lookup the
gateway allows for exactly two services (`builtin.COMPONENT_SERVICES`):

    mail_accounts()  -> [{credential, account, kind, operational, send_only,
                          missing, error, email}]   names and addresses, never a secret
    mail_exec({account, argv, files, timeout})
                     -> {returncode, stdout, stderr}  google-pack materialises
                                                       its own XOAUTH2 files

When google-pack is not serving (not installed, `google: native`, failed), a
Google account is unknown here and its calls are refused: fail closed, never
the gateway's native client.

`vault` below is the one object the mail code talks to about credentials, with
the three calls the gateway's `email.py` used (`has_credential`,
`read_internal`, `store_names`): the code moved from the gateway keeps its
shape, and the suites that patch `email.vault.*` run unchanged against it.
"""
from __future__ import annotations

import logging
from typing import Any

HOST: Any = None

LOG = logging.getLogger("clodia-tools.component.comms-pack.mail")

#: The vault prefix of the IMAP/SMTP mailboxes (the gateway's custody).
MAILBOX = "mailbox_"
#: The vault prefixes of Google accounts: google-pack's, never read here.
GOOGLE_PREFIXES = ("google_", "gmail_")
#: The pack whose gateway component serves Gmail, and the two services asked.
GOOGLE_PACK = "google-pack"
GMAIL_ACCOUNTS = "mail_accounts"
GMAIL_EXEC = "mail_exec"


def get() -> Any:
    if HOST is None:
        raise RuntimeError("comms-pack component: no host (register() not called)")
    return HOST


# ── Gmail: google-pack's component ─────────────────────────────────────────

def google_service(name: str):
    """google-pack's service `name` (a callable), or None when google-pack's
    gateway component is not serving on this instance."""
    try:
        return get().service(GOOGLE_PACK, name)
    except Exception as e:  # noqa: BLE001 — not granted / not serving: absent
        LOG.warning("mail: google-pack service %s not reachable (%s)", name,
                    type(e).__name__)
        return None


def google_rows() -> list[dict]:
    """google-pack's rows for the Google mail accounts, or [] when it is not
    serving. Each row: the gateway's diagnostics shape plus `email` (the
    bundle's address as written there)."""
    svc = google_service(GMAIL_ACCOUNTS)
    if svc is None:
        return []
    try:
        rows = svc() or []
    except Exception as e:  # noqa: BLE001 — a listing, never a reason to fail
        LOG.error("mail: google-pack mail_accounts failed (%s)", type(e).__name__)
        return []
    return [dict(r) for r in rows if isinstance(r, dict)
            and str(r.get("credential") or "").startswith(GOOGLE_PREFIXES)]


def google_unavailable() -> str:
    """Why Gmail accounts are not served here, or "" when google-pack is."""
    if google_service(GMAIL_ACCOUNTS) is not None:
        return ""
    return ("Gmail accounts are served by google-pack's gateway component, which "
            "is not serving on this instance (not installed, `google: native`, "
            "or failed — see GET /internal/components)")


# ── the credentials the mail code sees ─────────────────────────────────────

class _Vault:
    """`has_credential` / `read_internal` / `store_names`, as `email.py` used
    them, answered from the two places this component may ask:

    * `mailbox_<acct>` — the component's own scope, `host.credentials`;
    * `google_<acct>` / `gmail_<acct>` — google-pack's rows: existence and the
      ADDRESS only. `read_internal` of a Google credential returns
      `{"email": <address>}` and nothing else: the refresh token stays with
      google-pack.
    """

    def _scope(self):
        return get().credentials(MAILBOX)

    def _google(self) -> dict[str, dict]:
        return {str(r["credential"]): r for r in google_rows()}

    def has_credential(self, name: str) -> bool:
        name = str(name or "")
        if name.startswith(MAILBOX):
            return bool(self._scope().has(name))
        if name.startswith(GOOGLE_PREFIXES):
            return name in self._google()
        return False

    def read_internal(self, name: str) -> dict:
        name = str(name or "")
        if name.startswith(MAILBOX):
            return dict(self._scope().read_internal(name))
        if name.startswith(GOOGLE_PREFIXES):
            row = self._google().get(name)
            if row is None:
                raise PermissionError(f"credential '{name}' not available")
            return {"email": row.get("email") or ""}
        raise PermissionError(f"'{name}' is not a mail credential of this component")

    def store_names(self) -> list[str]:
        return list(self._scope().list()) + list(self._google())

    def deposit(self, name: str, bundle: dict, *, cred_type: str,
                grant_agents=()) -> None:
        self._scope().deposit(name, bundle, cred_type=cred_type,
                              grant_agents=list(grant_agents))

    def remove(self, name: str) -> bool:
        return bool(self._scope().remove(name))


vault = _Vault()
