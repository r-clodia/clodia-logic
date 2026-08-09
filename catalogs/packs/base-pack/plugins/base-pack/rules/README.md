# Rules

A rule is a `.md` file with a `globs:` front-matter and a body of contextual
knowledge. The catalogue is runtime-neutral: the `agent_sdk` adapters decide how
to present rules to the agentic CLI or SDK they drive.

> **The specification wants this field gone** (§1.6: *no rules, no sandbox*).
> Rules survive as a live mechanism and are documented here so that what exists
> is legible — not as something to build on. Anything new belongs in a skill.

## What is actually here

This directory holds one rule, `topic-state-boundary.md`. An earlier version of
this document listed four others — `secrets-handling`, `git-commit-style`,
`python-style`, `skill-authoring` — that no longer exist. A catalogue that lists
absent entries is worse than an empty one: it makes a reader conclude they are
loaded somewhere else.

## Where rules live

| pack | meaning | path |
|---|---|---|
| `base-pack` | native rules, present in every installation | this directory, in git |
| `local-pack` | rules created or modified by the owner of an instance, local overrides included | `CLODIA_DATA/rules-catalog/` |
| `<name>-pack` | a pack installed for a business, customer or domain | usually `CLODIA_DATA/rules-catalog/<pack>/` |

```text
CLODIA_DATA/rules-catalog/
├── agent-server-fastapi.md        # flat rule → local-pack
└── acme-pack/                     # pack sub-directory → explicit pack, from the path
    ├── blog-voice.md
    └── next-conventions.md
```

As with skills, a data-catalogue rule's pack is determined by its **path**;
flat rules stay in `local-pack`. Packs imported through `/clodia/packs/import`
install their rules into the pack sub-directory.

A data-only rule may also declare its pack in the front-matter (`pack`,
`pack_id` or `packId`). Without a declaration the API falls back to
`local-pack`.

## Rules, skills, and the constitution

| | constitution | rules | skills |
|---|---|---|---|
| loaded | always, at start | on demand, by path glob | on demand, by name/description match |
| for | short facts that are always true | domain knowledge triggered by a path | active multi-step workflows |

## Resolution order

`server/agents/rule_sync.py` materialises every rule named in `agent.yaml.rules`
as `.agent/rules/<name>.md` inside the ephemeral workspace. It resolves, in
order:

1. qualified `<pack>/<rule>` → `/datadir/rules-catalog/<pack>/<rule>.md`
2. bare name → `/datadir/rules-catalog/<rule>.md` (flat)
3. bare name → `/datadir/rules-catalog/<pack>/<rule>.md` (first pack in order)
4. `/clodia/rules-catalog/<rule>.md` (base-pack)

That precedence is what makes a local override work: the data version wins where
it exists. `agent.yaml.rules` also accepts `<pack>/*` and `*`.

When a name exists in both catalogues, the read-only `/clodia/rules` API reports
`source: "both"`, the `available_packs`, and both `variants` — so an instance can
keep the native rule and its own customised version side by side.
