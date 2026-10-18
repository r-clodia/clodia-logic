# Changelog — comms-pack

Changelog of the `comms-pack` pack ([Keep a Changelog](https://keepachangelog.com/)
format, SemVer). The newest version is at the top; `pack.yaml` holds the current
one.

> **The record starts here.** Versions up to 1.1.0 went out without a changelog;
> they are not reconstructed below, because a changelog that invents its own
> past is worse than one that says where it begins.

## [2.1.0] — 2026-10-18
- **Telegram runs in the gateway as a component of this pack**
  (clodia-platform#522/#558). The MCP backend `telegram` (a stdio process per
  call, with the bot token in its environment) is gone: the same code
  (`gateway/telegram_core.py`) is served by `gateway/telegram_component.py`.
  Agent verbs unchanged (`telegram.inbox`, `.send`, `.send_file`, …); the
  relay's long-poll and downloads are the component's services, out of any
  agent's reach; the token is read from the gateway's vault only while
  `connectors: {telegram: pack}`; the state (offset, queues, leases) is the
  component's private file on the gateway's volume — known chat titles are
  learned again as chats write. **Needs clodia-tools ≥ 2.115.0.** After the
  Update, unmount the stale `telegram` backend (Tools → MCP).

## [2.0.1] — 2026-10-18
- **TLS verified** (clodia-platform#556, security): the IMAP/SMTP client checks
  the server certificate and hostname (`ssl.create_default_context()`); before,
  imaplib/smtplib's default context checked neither. Also in the mailbox
  login test, which sends the real password. **A mailbox on a server with a
  self-signed or mismatched certificate now fails** on every verb with
  `CERTIFICATE_VERIFY_FAILED`: there is no opt-out.
- **A reply goes to ONE address** (clodia-platform#559 review): the first
  address of the original `From`, the one the egress check judges. A `From`
  listing several addresses used to send the reply to all of them.
- **Non-ASCII attachment names** (clodia-platform#557) arrive named (RFC 2231).
- **Strict ingress** (clodia-platform#560): a list/search result that is not a
  list reveals nothing on a strict channel.

## [2.0.0] — 2026-10-17
- **Mail runs in the gateway as a component of this pack** (clodia-platform#558,
  owner decision 2026-10-16; needs clodia-tools ≥ 2.110.0 and clodia-logic
  ≥ 6.306.0; Gmail accounts need google-pack ≥ 2.1.0). New `components:` block
  with one `target: gateway` component, `gateway/` (entry `mail_component`),
  fetched by the gateway itself from r-clodia/clodia-logic's default branch,
  pinned by commit and loaded only on `connectors: {mail: pack}`:
  - the eight `email.*` verbs (schemas copied verbatim from the gateway),
    served through the gateway's normal `call_tool` path — whitelist, deny,
    unattended block, gates, the egress PDP on this pack's mappings, taint,
    audit. comms-pack owns the mail vocabulary, so it owns the verbs for EVERY
    account;
  - every decision the gateway took inside them, reading the perimeter from
    the gateway per call through the host: account resolution, the mailbox
    whitelist (`inbox:`/`outbox:`), strict ingress with its fail-closed audit
    record (#503, same refusal text — `main._denial_class` still classifies it
    as `sender_not_vetted`), agent attachments confined to the caller's
    scratch (#461), topic attachments in (`topic_files`) and out
    (`save_attachment`: `untrusted`, channel tainted, name sanitised #420), the
    reply recipient (service `pdp_arguments`: the gateway drops the caller's
    `to` and judges the reply by the original sender);
  - the IMAP/SMTP client (`email_client.py`, the gateway's
    `vendor/email_client.py` verbatim) run on ONE mailbox's `mailbox_<acct>`
    credential, written to a private 0700 directory removed after the call,
    with a minimal environment and no bytecode next to the pinned code. The
    credentials stay in the gateway's vault (`host.credentials("mailbox_")`,
    reachable only while the switch is on `pack`);
  - Google accounts are google-pack's: their credential never reaches this
    component, which asks google-pack's component (`mail_accounts`,
    `mail_exec`) through `host.service`. Without google-pack serving, a Google
    account is refused — never the gateway's native client;
  - the mailbox management of the Tools page as services the gateway's own
    routes call (`mailboxes_list`, `mailbox_add`, `mailbox_remove`,
    `test_mailboxes`), plus `diagnostics`, `system_mailboxes`,
    `mailbox_address` for the topic button and the Tools card;
  - legacy accounts (`secrets/email_config.json`) are listed and refused
    with the reason: they keep working on `mail: native` only.
  Code moved from clodia-tools: `server/tools/email.py`, the `email.*`
  branches and `_email_account`/`_topic_attachments`/`_agent_attachments`/
  `_reply_recipient` of `server/main.py`, the mailbox routes and the mailbox
  test of `server/tools_api.py`, `vendor/email_client.py`. The gateway keeps
  its native copy behind the switch until the removal step.
- **Transport removed**: the `mail-transport` backend (`mail_transport_mcp.py`,
  `mail_transport_core.py` and the plugin's `email_client.py`) is gone, and
  the `imap` provider declares no transport.
- **`unmapped:`** on `mail` for `email.folders` and `email.save_attachment`
  (no egress/source mapping, no taint row): the gateway serves a component
  verb only when the reviewed manifest classifies it. The PDP answers are the
  builtin's, unchanged.

## [1.4.0] — 2026-10-16
- **The pack declares the `mail` connector** (clodia-platform#523): the
  gateway's builtin `mail.yaml` (clodia-tools), moved verbatim in meaning —
  schemes `mailto`/`mailfrom`/`inbox`/`outbox` (all `lowercase`), the
  `email.send`/`email.reply`/`email.read` mappings, the admin-only
  `mailbox-link` topic action over `options: mailboxes`, the Tools card and the
  taint rows, labels and the gate's danger text included. Two additions only,
  both required for a non-builtin manifest: `forms:` (an email address) on
  `inbox` and `outbox`, which the topic action writes into, and
  `providers: [{id: imap, transport: {backend: mail-transport, tool: run}}]`.
- **The pack ships the IMAP/SMTP transport, not the verbs.** Unlike Telegram,
  the gateway keeps `email.*` and every decision — account resolution, mailbox
  whitelist, strict ingress and its audit (#503), topic attachments and taint,
  reply recipient, the vault. The new backend `mail-transport`
  (`plugins/comms-pack/mcp/mail_transport_mcp.py`, logic in the stdlib-only
  `mail_transport_core.py`) exposes one gateway-internal tool, `run`
  (`argv`, `files`, `timeout` → `{returncode, stdout, stderr}`): it writes that
  call's credential and attachments into a private 0700 temp dir (relative
  paths only; absolute paths, `..` and links refused; files 0600), runs the
  bundled `email_client.py` — a copy of the gateway's `vendor/email_client.py`
  — with `CLODIA_SECRETS_DIR` pointing there and a minimal environment, and
  always removes the dir, on failure and timeout too. stderr is trimmed and
  scrubbed of the secret values it was handed.
- **No mount-time secret**: the `mail-transport` entry in `.mcp.json` has no
  `${VAULT:…}` placeholder — the credential arrives per call, so the process
  never holds more than the mailbox of the call in progress.
- Off by default: nothing changes until the instance profile sets
  `connectors: {mail: pack}`. Mailboxes are still added from Tools → mailboxes.

## [1.3.0] — 2026-10-16
- **State is safe across processes** (review of clodia-logic#527): the gateway
  starts a stdio process per call, so the in-process lock let two concurrent
  `lease_acquire` both win the "exclusive" lease and two writers race on one
  temp file. State access is now serialised with a file lock, each writer uses
  its own temp file, and `getUpdates` is guarded by a separate lock held for the
  whole long-poll (a lazy refresh skips Telegram when someone else is polling).
- **The pack now carries the Telegram implementation too** (clodia-platform#522,
  phase 2): `plugins/comms-pack/mcp/telegram_mcp.py` is the gateway's
  `server/tools/telegram.py`, moved into the pack that declares the connector,
  and `.mcp.json` mounts it as the `telegram` MCP backend. The verbs come back
  to the agents under the same names, through the gateway's proxy — so after
  the whitelist, the deny lists, the gate, the egress check and the taint:
  nothing that used to be checked stops being checked.
- **The bot token never leaves the gateway's vault**: the mount injects it as
  `${VAULT:telegram_bot_token.token}` into the backend's environment. It does
  not pass through the model and it is not in this repository. The FIELD is
  part of the placeholder: the bare `${VAULT:entry}` form resolves the bundle's
  `value`, which a `kind: form` credential does not have, so the placeholder
  would survive literally and the backend would start with `${VAULT:…}` in
  place of the token — a fault invisible at mount time, visible only at the
  first call to api.telegram.org.
- **Channel and agent arrive as declared context** (`context:` in `pack.yaml`,
  one `{arg, value}` row each — the grammar the gateway's parser reads; written
  as a mapping the connector does not enter the registry and the block stays
  inert), filled and always overwritten by the gateway: a backend outside the gateway
  has no signed claim, and a context that the caller could choose would mean
  writing into another topic's group, or taking somebody else's lease.
- **The Tools card now matches the gateway's own manifest field by field**
  (`icon`, `setup_label`, `status_verb`, `blurb`, `scope`): when the per-instance
  switch moves to `pack`, the gateway's document leaves service and this card
  takes its place — a different copy here would be the page silently changing
  on migration day.
- Two verbs are for the gateway's relay and not for agents — `telegram.updates`
  (long-poll) and `telegram.download` — because Telegram allows one `getUpdates`
  consumer per bot: whoever holds it holds the queue. The `messaggero` seed
  denies them explicitly, since its grant is `telegram.*`.
- **`send_file` takes a `path`, not bytes**: the file lives in a topic, and
  reading it is platform work — the compartment and the audit of what crosses
  the boundary hang off that read. The manifest declares which arguments are
  the path and the bytes (`files:`), and the gateway delivers them.
- `telegram.updates` reports `chat_type` per message and `bot_username` with
  the response: the gateway needs both to recognise a join-by-link `/start
  <token>` on the way in — `claim` only accepts groups, and without the type a
  join in a group would land in the topic as an ordinary message.
- **The bound chat is resolved by the gateway too** (`defaults:`): `send`
  without a `chat_id` goes to the chat bound to the calling topic, and the
  bindings registry holds every connector's bindings — a pack process reading
  it itself would read everybody else's, with the gateway's uid. An explicit
  `chat_id` from the caller still wins: a default that overwrote would make it
  impossible to write anywhere else.
- Not moved, and on purpose: reading a topic file for `send_file`, the channel
  bindings and the join-by-link flow. They are platform, not connector. The
  pack reads no gateway state at all — a test asserts the source does not even
  name the bindings file.

## [1.2.0] — 2026-10-14
- **The pack declares the Telegram connector** (clodia-platform#522, epic #527):
  a `connectors:` block in `pack.yaml`, in the manifest grammar of #516. It
  describes the `tg:` scheme with its two forms of #363, the bot-token
  credential, the two verbs the PDP checks today, the reads that taint a
  channel, the topic button and the setup card.
- **It reproduces today's gateway, to the letter.** Patterns, danger text,
  destination types and the templates of the link action are copied from
  `egress.py`, `taint.py` and `topics_api.py`, not rewritten: the declaration is
  what #518 will be measured against when it deletes those tables, and a
  "better" form here would be a silent change of behaviour inside a migration
  that must be at parity.
- **Nothing reads it yet, and nothing changes.** The registry that replaces the
  hardcoded tables is #518, the generic endpoints #519, the webui #520. Until
  they land the gateway keeps using its own tables; the native Telegram code is
  untouched, and #522 stays open.
