# Changelog — google-pack

Changelog of the `google-pack` pack ([Keep a Changelog](https://keepachangelog.com/)
format, SemVer). The newest version is at the top; `pack.yaml` holds the current
one.

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
