# Changelog — comms-pack

Changelog of the `comms-pack` pack ([Keep a Changelog](https://keepachangelog.com/)
format, SemVer). The newest version is at the top; `pack.yaml` holds the current
one.

> **The record starts here.** Versions up to 1.1.0 went out without a changelog;
> they are not reconstructed below, because a changelog that invents its own
> past is worse than one that says where it begins.

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
