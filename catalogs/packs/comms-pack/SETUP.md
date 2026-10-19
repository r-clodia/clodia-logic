# SETUP — comms-pack (sysadmin runbook)

What to do after importing or updating the pack.

## 0. What it is made of

comms-pack 2.0.0 is a distribution of components (clodia-platform#558):

| component | where it runs | what it is |
|---|---|---|
| `gateway/` (`components:`, `target: gateway`) — `mail` | the gateway | the eight `email.*` verbs, mailbox whitelist, strict ingress, topic attachments, reply recipient, the mailbox services of the Tools page, the IMAP/SMTP client |
| `gateway/` (`components:`, `target: gateway`) — `telegram` (2.1.0) | the gateway | the `telegram.*` agent verbs, the relay's long-poll and downloads (services), the Bot API client; token from the gateway's vault, state in the component's private file |
| `plugins/comms-pack` | the agent-server | the skills (check-email, telegram-1to1, helpdesk) |
| `connectors:` | gateway PDP + webui | `telegram` and `mail`: schemes, mappings, taint, topic buttons, Tools cards |

The gateway component is never installed into the datadir: the gateway fetches
it from r-clodia/clodia-logic (default branch), pins the commit in its own
state directory and verifies it before every load.

Requirements: clodia-tools (gateway) ≥ 2.115.0 (Telegram component), clodia-logic ≥ 6.306.0; for
Gmail accounts on `mail: pack`, google-pack ≥ 2.1.0 on `google: pack`.

## 1. Plugin dependencies (`requires`)

None beyond the gateway image.

## 2. MCP servers

None since 2.1.0. Telegram, the MCP backend `telegram` up to 2.0.x, is a
gateway component (clodia-platform#522/#558): after the Update, unmount the
stale `telegram` backend from Tools → MCP. Connect the bot from Tools →
Telegram, as before: the token stays in the gateway's vault
(`telegram_bot_token`).

## 3. The gateway component

1. **First time**: after the Update the gateway pins the component by itself
   at its next reconcile (≤ 60 s; `CLODIA_COMPONENT_INTERVAL`). Check:

   ```sh
   curl -sS -H "Authorization: Bearer $OWNER_SESSION_TOKEN" \
        https://<gateway>/internal/components
   ```

   Its status must then be **`loaded`**.
2. **Every later Update**: the pin does NOT move by itself. A human admin asks
   the gateway to fetch the new head of `main`:

   ```sh
   curl -sS -X POST -H "Authorization: Bearer $OWNER_SESSION_TOKEN" \
        https://<gateway>/internal/components/comms-pack/refresh
   ```

**From 2.2.0** the component also offers the gateway three services,
`telegram_status`, `telegram_notify` and `telegram_chat_admins`; clodia-tools ≥ 2.119.0 relies on them
(it has no Telegram client of its own any more). Update comms-pack first.

## 4. The connectors

Telegram and mail are always comms-pack's (clodia-tools ≥ 2.124.0 retired the
`connectors: native|pack` switch, clodia-platform#526; a leftover `connectors:`
key in `profile.yaml` is ignored).

- **IMAP/SMTP mailboxes** (`mailbox_<acct>`) are served by the component. The
  credentials stay in the gateway's vault. Tools → mailboxes works as before.
- **Gmail accounts** (`google_<acct>`) are served through google-pack's
  component (google-pack ≥ 2.2.0 for the courier's `.eml` copy).
- **Legacy accounts** (`secrets/email_config.json`) are refused: move them to
  the vault (Tools → mailboxes).

Mailboxes are authorised per topic with the admin-only "Autorizza una casella"
button.

## 4b. Mail into a topic: the courier (from 2.4.0, clodia-platform#576)

The platform's courier brings mail into a topic only when the topic declares,
among ITS OWN ingress entries, both:

- `inbox:<mailbox>` — which mailbox it reads;
- `mailfrom:<sender>` — whose mail concerns this topic (`*@domain` works).

A mailbox feeds many topics; each gets only its declared senders. Global
`mailfrom:` entries and the topic's participants do not count. A topic with an
`inbox:` and no `mailfrom:` receives nothing. A sender added later brings its
mail from then on (what is already in the mailbox is recorded, not delivered).

Each delivery: an envelope post by `posta` (never the body), the attachments in
`files/posta-<key8>-<name>`, the whole message in
`local/mailbox/<date>-<key8>.eml` (messages over 25 MB are named, not copied) —
all untrusted. Needs clodia-tools ≥ 2.126.0 and clodia-logic ≥ 6.314.0; update
google-pack to ≥ 2.2.0 too, or Gmail mailboxes deliver nothing.

Limits to know:
- The sender is the `From:` header **as sent**, exactly one address; it is not
  authenticated (no SPF/DKIM check), so a forged `From:` of a declared sender
  passes like any mail client would show it. Declare senders, not trust.
- `mailfrom:*` opens the mailbox's whole mail to that one room: an explicit
  choice of the owner, never a default.

## 5. Verification

`GET /internal/components` → comms-pack/mail `loaded`. Then:

- `email.list`/`email.read` on an authorised IMAP mailbox, `email.send` to a
  whitelisted `mailto:`;
- `email.list` on a Google account by an agent with email tools (Gmail via
  google-pack);
- on a strict-ingress channel, a message from an undeclared sender is withheld
  from `email.list` and refused by `email.read` ("mittente non vagliato");
- Tools → mailboxes → Test: one line per mailbox;
- `telegram.status` reports the bot;
- a topic with `inbox:` + `mailfrom:<you>`: a mail from you arrives (envelope +
  `local/mailbox/*.eml`), one from another sender does not.

If the component is missing, refused or failed, every `email.*` verb is
refused (DENIED, naming the component) and the mailbox routes answer 503 —
there is no built-in fallback.
