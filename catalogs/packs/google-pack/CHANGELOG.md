# Changelog — google-pack

Changelog of the `google-pack` pack ([Keep a Changelog](https://keepachangelog.com/)
format, SemVer). The newest version is at the top; `pack.yaml` holds the current
one.

## [2.1.1] — 2026-10-17
- **No code change.** Republished so that an instance already on 2.1.0 gets an
  Update again: with clodia-logic ≥ 6.306.1 a human admin's Update also moves
  the gateway component's pin (clodia-platform#561). An instance whose
  `google` component is still pinned at 2.0.0's commit reaches the 2.1.0 code
  with this Update.

## [2.1.0] — 2026-10-17
- **Gmail for comms-pack's mail component** (clodia-platform#558; needs
  clodia-tools ≥ 2.110.0). With `connectors: {mail: pack}` the eight `email.*`
  verbs are comms-pack's gateway component's for every account; a Google
  account's credential stays in THIS component's scope, so comms-pack asks two
  new services, the only ones the gateway lets it look up
  (`builtin.COMPONENT_SERVICES`):
  - `mail_accounts()` — the Google mail accounts (`google_`/`gmail_`), in the
    shape of the gateway's diagnostics rows plus the bundle's address. Names and
    addresses only: never a token, a secret or a client id;
  - `mail_exec({account, argv, files, timeout})` — one run of the bundled client
    for `account`: comms-pack hands only that call's attachments (`att/…`; any
    other path is refused), and the XOAUTH2 files are written HERE, byte for
    byte as the gateway's `vault.materialize_google_oauth` writes them, then
    run by `gmail_core.run` (same per-call directory, `{dir}` rule, scrubbing
    and clean-up as `mail_run`).
  `mail_run` is unchanged and still serves `mail: native`.

## [2.0.0] — 2026-10-16
- **Google runs in the gateway as a component of this pack** (clodia-platform#558,
  owner decision 2026-10-16; needs clodia-tools ≥ 2.109.0 and clodia-logic
  ≥ 6.305.0). New `components:` block with one `target: gateway` component,
  `gateway/` (entry `google_component`), fetched by the gateway itself from
  r-clodia/clodia-logic's default branch, pinned by commit and loaded only on
  `connectors: {google: pack}`:
  - the 24 `gdrive/gsheets/gdocs/gcalendar` verbs (schemas copied verbatim
    from the gateway), served through the gateway's normal `call_tool` path —
    whitelist, deny, unattended block, gates, the egress PDP on this pack's
    mappings, taint, audit;
  - the folder/calendar perimeter (`gdrive_root`), reading the gateway's
    `gdrive_roots`, egress list, `gcal:` approvals and the topic's
    `drive_folders` per call through the host;
  - the Google REST client (`google_io`, stdlib, token refresh from the vault
    through the host, bearer never carried off its host on a redirect);
  - the OAuth consent and the app client as routes (`app`, `auth`, `connect`
    under `/tools/google-pack/`; the gateway's `/tools/google/*` forward to
    them), depositing the same `google_<acct>` / `app_google_oauth` bundles in
    the gateway's vault — no re-consent;
  - Gmail for `email.*` on Google accounts (`mail_run`, the former
    gmail-transport code and the vendored `email_client.py`, now run in the
    gateway's process, never writing bytecode next to the pinned code);
  - the services the gateway calls (`accounts`, `describe_folder`,
    `perimeter_changed`, `credential_changed`, `folder_labels`,
    `pdp_arguments`, `test_connection`, `mail_run`).
  Code moved from clodia-tools: `server/tools/{google_io,gdrive,gdrive_root,
  gdrive_labels,gsheets,gdocs,gcalendar}.py`, `server/google_oauth.py`, the
  Google routes of `server/tools_api.py`, the dispatchers and schemas of
  `server/main.py`. The gateway keeps its native copy behind the switch until
  the removal step.
- **Transports removed**: `google-transport` and `gmail-transport` (and
  `.mcp.json`) are gone; the `workspace` and `gmail` providers declare no
  transport.
- **`unmapped:`** on `google`, `google-docs` and `google-calendar` for the
  eleven verbs with no egress/source mapping and no taint row: the gateway
  serves a component verb only when the reviewed manifest classifies it. The
  PDP answers are the builtin's, unchanged.
- **Skill**: `topic-drive-sync` (keep a topic in sync with a Drive folder),
  copied from base-pack, whose copy stays until the removal step.

## [1.0.0] — 2026-10-16
- **First release** (clodia-platform#524, step 4). The pack declares the Google
  connectors and ships their transports; the verbs, the perimeter and the
  credentials stay in the gateway.
- **Four connectors, the gateway's builtin `google.yaml` moved** (clodia-tools
  2.107.0): `google` (namespace `gdrive`: the `gdrive:`/`gsheets:`/`gcal:`
  vocabulary, the `workspace` account provider, the `drive-folder` topic
  button, the Tools card) and the verb-only siblings `google-sheets`,
  `google-docs`, `google-calendar`. Every scheme, form, verb mapping, taint row,
  label and danger text is the builtin's, verbatim in meaning. Two differences,
  both required of a pack manifest: the `gdrive`/`gsheets` canonicalisation is
  rewritten in the pack regex subset (one rule per shape instead of an optional
  group — same answers, except that `drive/u/<n>/folders/` is recognised for
  n < 100), and the `workspace` provider declares its transport.
- **`google-transport`** (`plugins/google-pack/mcp/google_transport_mcp.py`,
  logic in the stdlib-only `google_transport_core.py`): one gateway-internal
  tool, `request`, implementing the `google.request` contract — `{access_token,
  api, version, method, path, params, body, media_b64, media_mime, download}` →
  `{status, json?, bytes_b64?}`. The request is framed exactly like the
  gateway's `google_io`: same API and upload roots, `alt=media`/`alt=json`
  (none on DELETE), `uploadType=multipart|media`, booleans as `true`/`false`,
  lists as repeated keys, the same multipart/related framing, 30 s timeout. A
  redirect never carries the bearer token to another host or to plain http.
  A non-2xx status is returned as an answer (the gateway raises its own
  `HttpError` and retries a 401 with a fresh token); every failure is reported
  with a generic message that never carries the token, the arguments or the
  URL. Paths are confined to the API root (no `?`, `#`, `//`, dot segments or
  control characters). No state between calls; the refresh token and the client
  secret never reach the process.
- **`mail-gmail`**: Gmail as a provider (`gmail`) of the mail vocabulary
  (`extends: mail`, namespace `gdrive`), with its own transport
  **`gmail-transport`** (`gmail_transport_mcp.py`/`gmail_transport_core.py`):
  the contract of comms-pack's `mail-transport` (`{argv, files, timeout}` →
  `{returncode, stdout, stderr}`), the same validation, private 0700 call
  directory and `{dir}`-only-in-`--attachment` rule, running a copy of the
  gateway's `vendor/email_client.py` on that call's XOAUTH2 files. stderr is
  also scrubbed of Google access tokens and bearer values the client mints
  during the call.
- **No credential of its own**: no vault entry is declared (the `google_`,
  `gmail_`, `gworkspace_` and `app_` prefixes stay the gateway's), and
  `.mcp.json` mounts both backends without `${VAULT:…}` placeholders. Google
  accounts are still connected from Tools; the switch needs no re-consent.
- Off by default: nothing changes until the instance profile sets
  `connectors: {google: pack}`.
