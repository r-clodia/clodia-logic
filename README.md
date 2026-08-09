# clodia-logic

The **substrate** of Clodia Agency: logic only, versioned, no data. It is the
build context of the container image and the material a clone of the agency
inherits.

> ### 📍 This is not the entry repository
>
> This repository is a **component** of Clodia Platform, not something you
> install on its own. Installation, quickstart, architecture, licence and the
> **risk warnings** live in:
>
> ### 👉 **[r-clodia/clodia-platform](https://github.com/r-clodia/clodia-platform)**
>
> Do not deploy from here: `clodia-platform` clones the component repositories,
> builds the images and orchestrates the stack. Before installing, read the
> as-is disclaimer and the **known defects** in the platform tracker —
> [open `security` issues](https://github.com/r-clodia/clodia-platform/issues?q=is%3Aissue+is%3Aopen+label%3Asecurity)
> and [`SECURITY.md`](https://github.com/r-clodia/clodia-platform/blob/main/SECURITY.md).
> The software is distributed **AS IS, without warranty**: you run it at your
> own risk.

## What it does

It runs the **spawns**. A seed is a type; a spawn is a live instance of it with
its own uid, its own scratch directory and its own identity. This component
mints that identity against the colony CA and signs every call to the gateway
with the claims that make a decision possible downstream: which spawn, which
room, which clearance, and the chain of principals the request came through.

It also serves the human-facing API — chats, topics, jobs, agents, packs — that
the web UI speaks to.

It holds **no vetoes**. Those live in
[`clodia-tools`](https://github.com/r-clodia/clodia-tools), in a separate
process and container, because a reference monitor sharing an address space with
what it monitors is a convention rather than a boundary.

## Layout

| path | what it holds |
|---|---|
| `server/` | the API, the spawn runtime, the colony PKI, the scheduler |
| `server/agents/` | seeds: loading, inheritance, synchronisation to the datadir, the boot-time boundary assertion |
| `server/sdk_runtime/` | the session that drives an agent SDK (`claude`, `codex`, `opencode`) |
| `catalogs/packs/` | the bundled packs — `base-pack`, `comms-pack`, `editorial-pack` |
| `catalogs/PACKS.md` | what a pack is, and what a plugin is |
| `providers/`, `routing/`, `hooks/` | inference providers, routing, hooks |
| `docker/` | the image |

The bundled seeds are `archseed` (abstract, the ancestor that holds the base
verbs and cannot be spawned), `clodia`, `ophelia`, `segretario`, `messaggero`,
`sysadmin`. An instance's own agents live only in its datadir, never here.

## Agent SDK

Task-bound agents declare their runtime in `agent.yaml` — `agent_sdk`
(`claude` | `codex` | `opencode`) plus `model`. The ephemeral workspace then
converts skills, rules and sandbox into the layout that runtime expects.

## What it does **not** hold

No data. Topics, secrets, an instance's hired agents and their memory live in
the mounted datadir, never in this repository.

## Governance

`main` is protected: pull request plus review. Agents may propose changes here
through fork and pull request; the merge is a human gate. No direct push to
`main`, no skipped CI, no force-push onto a reviewed branch.

## Rules

Not restated here. See
[`docs/specification.md`](https://github.com/r-clodia/clodia-platform/blob/main/docs/specification.md)
for the model and
[`docs/gap-analysis.md`](https://github.com/r-clodia/clodia-platform/blob/main/docs/gap-analysis.md)
for what the code enforces today, with the gaps named.

## Licence

Copyright (C) 2026 Davide Carboni.

GNU AGPL v3, with a commercial option: see [LICENSING.md](LICENSING.md).
Releases up to the `apache2-final` tag remain Apache 2.0.
