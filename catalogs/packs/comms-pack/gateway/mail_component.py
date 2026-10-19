"""comms-pack's gateway component: mail (IMAP/SMTP), run inside the gateway.

clodia-platform#558 (owner decision 2026-10-16): a pack is a distribution of
components, each mounted in its container. This is the `target: gateway` one
of comms-pack. The gateway fetches it from r-clodia/clodia-logic (default
branch, pinned commit), imports this module and calls `register(host)` — and,
from then on, on `connectors: {mail: pack}`:

- the eight `email.*` verbs are served from here, through the gateway's normal
  `call_tool` path (whitelist, deny, unattended block, gates, the egress PDP
  on the mappings of this pack's `mail` connector, taint, audit). comms-pack
  owns the mail VOCABULARY, so it owns the verbs, for every account;
- the decisions the gateway took inside those verbs run here, reading the
  perimeter from the gateway per call: the mailbox whitelist
  (`inbox:`/`outbox:`), strict ingress with its fail-closed audit record
  (#503), topic attachments in and out with their taint, the reply recipient
  (the `pdp_arguments` service);
- an IMAP/SMTP mailbox runs on its `mailbox_<acct>` credential, which stays in
  the gateway's vault and is reached through `host.credentials("mailbox_")`;
- a Google account runs on google-pack's component (`mail_accounts`,
  `mail_exec`, looked up through `host.service`): its credential never passes
  here, and without google-pack those accounts are refused, never served by
  the gateway's native client;
- the mailbox management of the Tools page (list, add, remove, test) is a set
  of services the gateway's own routes call.

On `native` (the default) the gateway keeps this component pinned but on
standby: none of this runs, and the gateway's own `email.py` serves mail.
"""
from __future__ import annotations

from typing import Any

from . import _host

#: The services the gateway calls (`components.service("comms-pack", name)`).
SERVICES = ("pdp_arguments", "diagnostics", "system_mailboxes", "mailbox_address",
            "mailboxes_list", "mailbox_add", "mailbox_remove", "test_mailboxes",
            "courier_envelopes", "courier_attachment", "courier_eml")


def register(host: Any) -> None:
    _host.HOST = host
    # Every module imported HERE, not at first use: an import error disables
    # the component at load (logged, its verbs denied), not on a live call.
    from . import mail, mailboxes, schemas, verbs

    for tool in schemas.TOOLS:
        host.register_verb(tool, verbs.handler_for(tool.name))
    # Late-bound (the module attribute is read at each call), like the verbs'
    # handlers: one implementation per name, wherever it is reached from.
    host.register_service("pdp_arguments",
                          lambda verb, arguments: verbs.pdp_arguments(verb, arguments))
    host.register_service("diagnostics", lambda: mail.credential_diagnostics())
    host.register_service("system_mailboxes", lambda: mail.system_mailboxes())
    host.register_service("mailbox_address", lambda account: mail.mailbox_address(account))
    host.register_service("mailboxes_list", lambda: mailboxes.mailboxes_list())
    host.register_service("mailbox_add", lambda body: mailboxes.mailbox_add(body))
    host.register_service("mailbox_remove", lambda account: mailboxes.mailbox_remove(account))
    host.register_service("test_mailboxes", lambda: mailboxes.test_mailboxes())
    # The courier (#531): envelopes and attachment bytes, never the body.
    host.register_service("courier_envelopes", lambda a: mail.courier_envelopes(
        str(a.get("account") or ""), str(a.get("folder") or "INBOX"),
        int(a.get("limit") or 20), list(a.get("skip") or []),
        bool(a.get("keys_only"))))
    host.register_service("courier_attachment", lambda a: mail.courier_attachment(
        str(a.get("account") or ""), str(a.get("email_id") or ""),
        str(a.get("filename") or ""), str(a.get("folder") or "INBOX")))
    # #576: the whole message, for the topic's `.eml` copy.
    host.register_service("courier_eml", lambda a: mail.courier_eml(
        str(a.get("account") or ""), str(a.get("email_id") or ""),
        str(a.get("folder") or "INBOX")))
