# SETUP — google-pack (sysadmin runbook)

What to do after importing or updating the pack.

## 0. Installing it

google-pack is bundled with clodia-logic (`catalogs/packs/google-pack`). On a
fresh datadir `init-datadir.sh` registers its manifest like every bundled pack;
then **Update** it from the Packs page to install the plugin and mount its MCP
servers. On an existing datadir that predates the pack, register the manifest
once and then Update:

```sh
mkdir -p "$CLODIA_DATA/packs/google-pack"
cp catalogs/packs/google-pack/pack.yaml "$CLODIA_DATA/packs/google-pack/pack.yaml"
```

`google-pack` is a reserved first-party name: it cannot be imported from a zip
or a third-party URL.

## 1. Plugin dependencies (`requires`)

None beyond the gateway image: both backends are stdlib Python plus the `mcp`
package the gateway already has.

## 2. MCP servers

`plugins/google-pack/.mcp.json` mounts two stdio backends. **Nothing to
configure** for either: neither has a mount-time credential.

- **`google-transport`** — the transport of the `google` connector. The
  gateway calls its single tool `request` once per Google API call, with a
  short-lived access token; the refresh token and the OAuth client stay in the
  gateway's vault.
- **`gmail-transport`** — the transport of the `mail-gmail` provider. The
  gateway calls its single tool `run` with that one call's XOAUTH2 files.

Both tools are gateway-internal and hidden from agents, who keep calling
`gdrive.*`, `gsheets.*`, `gdocs.*`, `gcalendar.*` and `email.*`.

## 3. Turning the connectors on

Nothing runs until the instance profile switches Google to the pack:

```yaml
# profile.yaml
connectors:
  google: pack
```

The one switch moves the four Google connectors (`google`, `google-sheets`,
`google-docs`, `google-calendar`) and Gmail together. With `native` (the
default) the gateway keeps using its built-in code. Gmail accounts follow
`google`; IMAP mailboxes keep following `mail` (comms-pack).

**No re-consent.** Google accounts are still connected from Tools → Google,
and their bundles (`google_<account>`) stay in the gateway's vault in both
positions. Drive folders are still authorised per topic with the
"Aggiungi una cartella Drive" button, and a topic's `drive_folders` still
narrows `gdrive.*` in that room.

## 4. Verification

With `google: pack`:

- `gdrive.list` and `gdrive.upload` confined to an authorised folder behave as
  on `native` (an upload without destination is still refused `OutsideRoot`);
- `gsheets.append_rows` on an authorised sheet, `gcalendar.list_events` on an
  approved `gcal:` calendar;
- the `topic-drive-sync` skill on a real topic, and the folder labels;
- for Gmail (`email.list`/`email.send` on a `google_` account): the same
  results as on `native`;
- no `ya29.`/`1//` in the gateway's or the pack's logs.

If google-pack is missing, refused or its backends are not mounted, every
Google verb is refused — never served by the built-in code. Set
`connectors: {google: native}` to go back.
