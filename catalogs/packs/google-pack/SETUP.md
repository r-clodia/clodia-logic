# SETUP — google-pack (sysadmin runbook)

What to do after importing or updating the pack.

## 0. What it is made of

google-pack 2.0.0 is a distribution of components (clodia-platform#558):

| component | where it runs | what it is |
|---|---|---|
| `gateway/` (`components:`, `target: gateway`) | the gateway | the 24 Google verbs, the folder/calendar perimeter, the OAuth consent routes, Gmail, the services the gateway calls |
| `plugins/google-pack` | the agent-server | the `topic-drive-sync` skill |
| `connectors:` | gateway PDP + webui | schemes, mappings, taint, the "Aggiungi una cartella Drive" topic button, the Tools card |

The gateway component is never installed into the datadir: the gateway fetches
it from r-clodia/clodia-logic (default branch), pins the commit in its own
state directory and verifies it before every load.

## 1. Installing / updating

Requirements: clodia-tools (gateway) ≥ 2.109.0 (≥ 2.110.0 for Gmail with
`mail: pack`), clodia-logic ≥ 6.305.0 (an
older agent-server drops `components:` and `first_party` from the installed
manifest, and the gateway then never sees the component).

google-pack is bundled with clodia-logic (`catalogs/packs/google-pack`). On a
fresh datadir `init-datadir.sh` registers its manifest like every bundled pack;
then **Update** it from the Packs page. On an existing datadir that predates
the pack, register the manifest once and then Update:

```sh
mkdir -p "$CLODIA_DATA/packs/google-pack"
cp catalogs/packs/google-pack/pack.yaml "$CLODIA_DATA/packs/google-pack/pack.yaml"
```

`google-pack` is a reserved first-party name: it cannot be imported from a zip
or a third-party URL.

## 2. The gateway component

1. **First time**: after the Update the gateway pins the component by itself
   at its next reconcile (≤ 60 s; `CLODIA_COMPONENT_INTERVAL`). Check:

   ```sh
   curl -sS -H "Authorization: Bearer $OWNER_SESSION_TOKEN" \
        https://<gateway>/internal/components
   ```

   With the switch still on `native` its status is **`standby`** (pinned, not
   imported, nothing served by it): expected.
2. **Every later Update**: the pin does NOT move by itself. A human admin asks
   the gateway to fetch the new head of `main` (the agent-server cannot: it is
   refused there by design). Until the webui has a button, with the owner's
   own session token:

   ```sh
   curl -sS -X POST -H "Authorization: Bearer $OWNER_SESSION_TOKEN" \
        https://<gateway>/internal/components/google-pack/refresh
   ```

   The Update answer names this call (`gateway_components.refresh`).

No MCP server to mount, no plugin dependency, no credential to configure.

## 3. Turning it on

```yaml
# $CLODIA_DATA/profile.yaml
connectors:
  google: pack
```

then restart the gateway (the profile is read at start; the first reconcile
at boot loads the component). The one switch moves the four Google connectors
(`google`, `google-sheets`, `google-docs`, `google-calendar`) and Gmail
together. With `native` (the default) the gateway keeps using its built-in
code and the component stays on standby. Gmail accounts follow `google`; IMAP
mailboxes keep following `mail` (comms-pack). With `mail: pack` (comms-pack
≥ 2.0.0) the `email.*` verbs are comms-pack's component's for every account,
and a Google account's mail runs here through `mail_accounts`/`mail_exec`
(2.1.0): without `google: pack` it is refused.

**No re-consent.** The Google credentials (`google_<account>`,
`app_google_oauth`, legacy `gworkspace_`/`gmail_`) stay in the gateway's vault
with the same names and shapes; the component reaches them through the gateway
only while the switch is on `pack`. Tools → Google keeps working: the
gateway's `/tools/google/*` forward to the component's routes. Drive folders
are still authorised per topic with the "Aggiungi una cartella Drive" button,
and a topic's `drive_folders` still narrows `gdrive.*` in that room.

## 4. Verification

`GET /internal/components` → google-pack `loaded`, 24 verbs, routes
`app`/`auth`/`connect`. Then, with `google: pack`:

- `gdrive.list` and `gdrive.upload` confined to an authorised folder behave as
  on `native` (an upload without destination is still refused `OutsideRoot`);
- `gsheets.append_rows` on an authorised sheet, `gcalendar.list_events` on an
  approved `gcal:` calendar;
- `gdrive.update` of a file outside the whitelisted folders is DENIED by the
  egress check (the parent lookup is the component's);
- the `topic-drive-sync` skill on a real topic, and the folder labels in the
  whitelist page;
- Tools → Google → Test: green for each connected account;
- Gmail (`email.list`/`email.send` on a `google_` account): the same results as
  on `native`;
- no `ya29.`/`1//` in the gateway's logs.

If the component is missing, refused or failed, every Google verb is refused
(DENIED, naming the component) — never served by the built-in code. Set
`connectors: {google: native}` and restart the gateway to go back.
