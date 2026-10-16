# SETUP — comms-pack (sysadmin runbook)

What to do after importing or updating the pack.

## 1. Plugin dependencies (`requires`)

None beyond the gateway image: both backends are stdlib Python plus the `mcp`
package the gateway already has.

## 2. MCP servers

`plugins/comms-pack/.mcp.json` mounts two stdio backends:

- **`telegram`** — the Telegram connector (clodia-platform#522). The bot token
  is injected from the gateway's vault (`${VAULT:telegram_bot_token.token}`);
  connect the bot from Tools → Telegram.
- **`mail-transport`** — the IMAP/SMTP transport of the `mail` connector
  (clodia-platform#523). **Nothing to configure**: it has no mount-time
  credential, the gateway hands it each call's mailbox credential as a file.
  Its single tool `run` is gateway-internal and hidden from agents.

## 3. Turning the connectors on

Both are off until the instance profile switches them:

```yaml
# profile.yaml
connectors:
  telegram: pack
  mail: pack
```

With `native` (the default) the gateway keeps using its built-in code. Mailboxes
are still added and removed from Tools → mailboxes, and authorised per topic
with the admin-only "Autorizza una casella" button, whichever position the
switch is in.

## 4. Verification

- `mail: pack`: `email.list` on an authorised mailbox returns messages, and
  `email.send` to a whitelisted `mailto:` delivers (same results as `native`).
- `telegram: pack`: `telegram.status` reports the bot.
