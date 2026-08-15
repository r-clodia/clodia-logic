# Changelog — base-pack

Changelog of the `base-pack` pack ([Keep a Changelog](https://keepachangelog.com/)
format, SemVer). The newest version is at the top; `pack.yaml` holds the current
one.

> **The record has a hole.** `pack.yaml` reached 7.5.0 while this file stopped at
> 7.0.0: five releases went out without an entry. They are recovered below from
> the git history rather than invented, and marked as such — a changelog that
> quietly fills its own gaps is worse than one that admits them.

## [7.9.0] — 2026-08-14
- **`clodia` gets `web.fetch` and `email.send` back.** On 6 Aug `email.*` was
  excluded in one block as "outbound"; the reason held but the remedy sat in the
  wrong place — a permission taken from one agent and left with another does not
  reduce the colony's authority, it moves it, and the daily digest job was left
  without either half of its trade. Confinement belongs to DESTINATION and
  SOURCE (`egress_allow` / `ingress`, in the gateway-only config), which is what
  `egress.py` states in its own header when it records that per-agent verb
  reduction was *measured* to be nearly worthless on its own.
- **Still excluded, and now for a stated reason**: `email.list/read/search` (the
  inbound post is the courier's trade, with a different sender each message) and
  `email.reply` — its recipient is not in the arguments, it comes from the
  message being replied to, so `egress` cannot read the destination at all.
- **`WebSearch`/`WebFetch` stay out of `native_tools`.** The provider runs them
  inside the API conversation, where no rule of ours is consulted: keeping them
  next to `web.fetch` would be a service door beside the controlled one.
  Requires clodia-tools ≥ 1.90.0, which is where `web.fetch` lands.

## [7.7.0] — 2026-08-12
- **Agent type vocabulary reduced to `bot | human`.** Base seeds now declare
  `type: bot`; legacy `normal` and `super` still parse as `bot`, but the
  registry/API emit the canonical value. Native protected seeds keep protection
  via `immutable: true`, not via a third agent class.

## [7.6.0] — 2026-08-09
- **`workflows.*` removed from the seeds.** `clodia` carried `workflows.list`
  and `workflows.status`, `sysadmin` the whole namespace. The engine is gone
  (decided 6 Aug, done 9 Aug), so those grants named verbs that no longer exist
  — a permission on a missing verb never fires and keeps saying something false
  about the surface of control.
- **`trello.*` removed** from the trifecta catalogue for the same reason.
- **`archseed`** is part of the pack: abstract, not spawnable, holding the base
  verbs every seed inherits.

## [7.1.0 – 7.5.0] — recovered from git, 2026-08-09
Entries reconstructed from the commits that moved `pack.yaml`; the wording is a
summary, not the original text.
- `segretario` gains a fallback introduction when it enters a topic (#252).
- `sysadmin` declares **absolute denies** for the vault and the secrets, and its
  prompt says what protects them (#217).
- `clodia`'s system prompt states the trade plainly: build the team first, then
  facilitate (#216).
- `AgentSpec.gated_tools`, propagated to the gateway at registration (#213).
- `messaggero` gets its file verbs back; `segretario` keeps three (#212).

## [7.0.0] — 2026-07-29
- **base-pack on a diet:** `base-pack/*` now expands only to the cross-cutting
  platform primitives — `topic-management`, `topic-files`, `topic-drive-sync`,
  `multiagent-collaboration`, `team-composition`.
- **New first-party packs:** editorial skills moved to `editorial-pack`
  (`article-spec`, `fact-check`, `editorial-review`), communication and support
  skills to `comms-pack` (`check-email`, `telegram-1to1`, `helpdesk`).
- **Native seeds de-wildcarded by role:** super-agents carry explicit
  first-party packs, `sysadmin` carries `comms-pack/helpdesk`, `messaggero` the
  comms skills.

## [6.9.0] — 2026-07-25
- **`sysadmin` — HTTP POST under supervision:** grants `web.post`, a verb
  separate from web reading and gated on every single invocation. The prompt
  requires an explicit destination and purpose, and forbids working around
  gates and limits.

## [6.8.0] — 2026-07-24
- **Skill `multiagent-collaboration`:** encodes teamwork inside a channel — work
  towards *goals*, not commands, and when a tool, grant or skill is missing,
  look in the channel for whoever can help (`runtime.agents`) and bring them in.
  Tag convention: `@agent` is a direct request (active; N tags → N agents),
  `$agent` a soft mention (the other decides whether to step in, otherwise a
  brief acknowledgement). The convention is also injected into every channel
  turn by the core.

## [6.7.0] — 2026-07-24
- **`sysadmin` — access to topic FILES under the ordinary rules.** Reverses the
  absolute ban: `sysadmin` now holds `topic.*` and reads and writes topic files
  like any other agent. Access is enforced by the gateway on two axes —
  **participant** of the topic, and **clearance ≥ tier**; on a topic it does not
  participate in, the **cross-topic gate** fires (owner's approval). No raw
  filesystem: as for `messaggero`, the verbs are the only way to the files.
  `topic.post_message` stays with the super-agents and `messaggero`.

## [6.6.0] — 2026-07-23
- **`sysadmin` — topic context from the widget.** When a user opens support
  while on a topic, the widget tells `sysadmin` which one (a hidden comment at
  the head of the message) and `sysadmin` can inspect it with
  **`runtime.inspect_topic(tier, name)`** — metadata, agents, latest messages.
  Bound by **clearance**: only if `sysadmin`'s effective SEAL ≥ the topic's tier,
  so confidential topics above its clearance stay invisible (403). This relaxes
  "never reads a topic's content" into "only within its clearance".
- **`check-email`:** the job must be created with `agent = messaggero` (itself),
  explicit in `jobs.propose`; the fire then runs as `messaggero`, which holds
  `topic.post_message`, not as `clodia`.
- **`janitor`:** every trace removed; the support widget answers as `sysadmin`.

## [6.5.1] — 2026-07-23
- **Skill `check-email`** (replaces `email-reconcile`, without the ledger):
  on request, `messaggero` creates a **job** that checks a mailbox every T,
  filters by subject and sender, and on a match **posts into the topic** with
  `topic.post_message` and an **@mention** of whoever should pick it up. Each
  fire is a short turn — no blocking listener, no state to keep.
- **`messaggero`:** gains `base-pack/check-email`, `jobs.propose` and `topic.*`.

## [6.5.0] — 2026-07-23
- **`janitor` and `sysadmin` consolidated into one seed, `sysadmin`** (platform
  steward). It absorbs `janitor`'s front-of-house role — the **support widget**,
  UI guidance, the `goto` marker, integration guidance — *and* performs
  platform-ops: unlike `janitor` it does not escalate, it executes, with the
  mutations gated. Adds `app_runtime.get/list/health` and the `helpdesk`
  capability. **`janitor` removed** from the pack; `helpdesk.agent` now defaults
  to `sysadmin`.
- A seed can read a pack's `SETUP.md` and run its provisioning (dependencies,
  MCP servers, `rag_collections`).

## [6.4.4] — 2026-07-23
- **Skill `email-reconcile`:** a job-driven routine that reconciles **incoming**
  mail into topics deterministically — a topic receives only replies to threads
  it started, matched on `In-Reply-To`/`References` against a ledger in the
  seed's memory. No content-based routing or triage, and no blocking listener:
  a short routine per turn, with the listening done by a periodic job.
- **`messaggero`:** gains `base-pack/email-reconcile`.

## [6.4.3] — 2026-07-23
- **`sysadmin` → full platform-ops:** `tool_permissions` extended to `agents.*`,
  `integrations.*`, `jobs.*`, `profile.*`, `providers.*`, `runtime.*` and
  `settings.*`, alongside `packs.*`, `fs.list_dir` and `logs.tail`. Nearly every
  mutation stays gated. `runtime.*` exposes topics and chats as **metadata
  only** — content is protected by `deny_read` plus clearance.

## [6.4.2] — 2026-07-22
- **`segretario` → `gemma-4-26b-a4b-it`** (Scaleway) instead of
  `mistral-small-24b`: under `tool_choice=auto` the latter did not call the
  verbs at all — it wrote prose about them. Added a blunt line to the prompt:
  act with the **tools**, not with the chat.

## [6.4.1] — 2026-07-22
- **`sysadmin`: verb `runtime.restart_agent`** — a targeted restart of an
  agent's live sessions when its runtime is stuck; history and data persist.
  Not gated.

## [6.4.0] — 2026-07-22
- **New agent `segretario`:** the topic's minute-taker — summary, TLDR, next
  steps — with write access to the state of the topic it participates in, and
  nothing else. A default participant.
- **Pack versioning and updates** from the Packs view: check for updates and
  update from the GitHub upstream, replacing seeds, skills and MCP servers, then
  restarting the affected agents.
