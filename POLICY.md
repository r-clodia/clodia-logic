# Where this component's rules are written

This file used to restate the agent-server's operating rules. It had stopped
describing this software some generations ago: it spoke of a **single**
long-running session for one workspace, a registry of external daemons, a
drag-and-drop upload into a fixed directory, and a subprocess with full access
to that workspace "because it is Clodia". The header claimed version 5.11.0
while a section below said 1.1-rc and the code was past 6.16.

It also named the maintainer's own machine path five times — a private path in
a public repository, which is its own reason to remove a file rather than
refresh it.

A restatement of a rule is a **second copy of that rule**, and the copy that
drifts is always the one that only explains. So the restatement is gone.

## The rules

| what you want to know | where it is written |
|---|---|
| the model — scopes, authority, gates, tiers, the perimeter | [`docs/specification.md`](https://github.com/r-clodia/clodia-platform/blob/main/docs/specification.md) |
| what the code actually enforces today, and what it does not | [`docs/gap-analysis.md`](https://github.com/r-clodia/clodia-platform/blob/main/docs/gap-analysis.md) |
| the threats this design answers, and the ones it does not | [`docs/threat-model.md`](https://github.com/r-clodia/clodia-platform/blob/main/docs/threat-model.md) |
| the security posture, mapped to ISO/IEC 27001:2022 Annex A | [`SECURITY.md`](https://github.com/r-clodia/clodia-platform/blob/main/SECURITY.md) |
| how the words are used — seed, spawn, scope, tier | [`docs/vocabulary.md`](https://github.com/r-clodia/clodia-platform/blob/main/docs/vocabulary.md) |
| what a pack is, and what a plugin is | [`catalogs/PACKS.md`](catalogs/PACKS.md) |
| the HTTP surface | `GET /openapi.json` on a running instance |

## What this component is

The **substrate**: the logic that a colony is built from, and the thing a clone
inherits. It runs agent spawns, mints their identities against the colony CA,
signs every gateway call with the claims that make a decision possible — which
spawn, which room, which clearance — and serves the human-facing API the web UI
speaks to.

It holds no vetoes. Those live in `clodia-tools`, in a separate process and a
separate container, because a reference monitor that shares an address space
with the thing it monitors is a convention rather than a boundary.

## Why the code is the authority here

Each rule is enforced in one place and carries its reason in its own docstring,
beside the decision it governs: `server/api/gate.py` decides who has standing to
unblock a gate and says what the action crosses; `server/agents/inheritance.py`
resolves what a seed inherits; `server/agents/boundary_check.py` asserts **from
inside a spawn, at boot** that it cannot reach the vault, the topic store or the
seeds. Reading those answers a question about behaviour more reliably than a
document about them.
