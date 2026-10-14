# Changelog — comms-pack

Changelog of the `comms-pack` pack ([Keep a Changelog](https://keepachangelog.com/)
format, SemVer). The newest version is at the top; `pack.yaml` holds the current
one.

> **The record starts here.** Versions up to 1.1.0 went out without a changelog;
> they are not reconstructed below, because a changelog that invents its own
> past is worse than one that says where it begins.

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
