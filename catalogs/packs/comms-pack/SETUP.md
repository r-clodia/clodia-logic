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

   With the switch still on `native` its status is **`standby`** (pinned, not
   imported, nothing served by it): expected.
2. **Every later Update**: the pin does NOT move by itself. A human admin asks
   the gateway to fetch the new head of `main`:

   ```sh
   curl -sS -X POST -H "Authorization: Bearer $OWNER_SESSION_TOKEN" \
        https://<gateway>/internal/components/comms-pack/refresh
   ```

**From 2.2.0** the component also offers the gateway three services,
`telegram_status`, `telegram_notify` and `telegram_chat_admins`; clodia-tools ≥ 2.119.0 relies on them
(it has no Telegram client of its own any more). Update comms-pack first.

## 4. Turning the connectors on

Both are off until the instance profile switches them:

```yaml
# $CLODIA_DATA/profile.yaml
connectors:
  telegram: pack
  mail: pack
```

then restart the gateway. With `native` (the default) the gateway keeps using
its built-in code and the mail component stays on standby.

On `mail: pack`:
- **IMAP/SMTP mailboxes** (`mailbox_<acct>`) are served by the component. The
  credentials stay in the gateway's vault with the same names: nothing to
  re-enter. Tools → mailboxes keeps working (same URLs, same authentication;
  the gateway's routes call the component).
- **Gmail accounts** (`google_<acct>`) are served through google-pack's
  component: they need google-pack ≥ 2.1.0 and `google: pack`. Otherwise they
  are refused (the error names google-pack).
- **Legacy accounts** (`secrets/email_config.json`) are refused: move them to
  the vault (Tools → mailboxes) first, or stay on `native`.

Mailboxes are authorised per topic with the admin-only "Autorizza una casella"
button, whichever position the switch is in.

## 5. Verification

`GET /internal/components` → comms-pack/mail `loaded`, 8 verbs. Then, with
`mail: pack`:

- `email.list`/`email.read` on an authorised IMAP mailbox, `email.send` to a
  whitelisted `mailto:` (same results as `native`);
- `email.list` on a Google account by an agent with email tools (Gmail via
  google-pack);
- on a strict-ingress channel, a message from an undeclared sender is withheld
  from `email.list` and refused by `email.read` ("mittente non vagliato");
- Tools → mailboxes → Test: one line per mailbox;
- `telegram: pack`: `telegram.status` reports the bot.

If the component is missing, refused or failed, every `email.*` verb is
refused (DENIED, naming the component) and the mailbox routes answer 503 —
never served by the built-in code. Set `connectors: {mail: native}` and
restart the gateway to go back.
