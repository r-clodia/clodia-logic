# Changelog — base-pack

Changelog of the `base-pack` pack ([Keep a Changelog](https://keepachangelog.com/)
format, SemVer). The newest version is at the top; `pack.yaml` holds the current
one.

> **The record has a hole.** `pack.yaml` reached 7.5.0 while this file stopped at
> 7.0.0: five releases went out without an entry. They are recovered below from
> the git history rather than invented, and marked as such — a changelog that
> quietly fills its own gaps is worse than one that admits them.

## [7.26.0] — 2026-09-24
- **Clodia moves to Claude Opus 5.5** (`claude-opus-5-5`, clodia-platform#392).
  Owner decision: every Opus seed runs Opus 5.5 on every provider. On Bedrock
  it resolves to the EU geo inference profile `eu.anthropic.claude-opus-5-5`.

## [7.25.0] — 2026-09-24
- **Soft mentions removed** (clodia-platform#391). `@name` is the only mention
  and always opens a turn; `$name` is no longer a citation: `$` belongs to
  composer aliases only (e.g. `$recap`). Clodia's seed and the
  `multiagent-collaboration` skill drop the `$` convention: a report that
  names an agent without calling it uses the plain name.

## [7.24.0] — 2026-09-24
- **Ship model: only Clodia/Segretario talk to the user and hand out work**
  (clodia-platform#389). Paradigm shift, decided by the owner: a specialist
  agent no longer auto-elects itself to answer an unaddressed channel
  message based on semantic relevance — that authority now belongs
  exclusively to the declared coordinator (Clodia, or Segretario when she is
  absent or the scope's tier exceeds her clearance; `coordinator.pick`,
  unchanged). Specialists never mention each other — the only mentions they
  produce are reports/escalations to Clodia/Segretario — and never address
  the user directly unless the user tagged them by name. `platform-core.md`
  (Principle 4) and the `multiagent-collaboration` skill are rewritten
  accordingly; Clodia's seed gets an explicit closing mandate, Segretario's
  `[COORDINAMENTO]` exception is extended to full complex-task orchestration
  when she is the fallback.
- Relevance scoring (`responder_routing.py`) is disabled as a deciding
  authority in `channels.py:_pick_responder()` but kept alive as an advisory
  signal recorded in the routing trace — not removed, in case a future
  delegation-hint use case wants it.

## [7.23.0] — 2026-09-23
- **Skill `topic-files` documenta la cartella condivisa Mac↔container**
  (`local/<nome>/`, clodia-platform#381): un agente che la incontra in
  `topic.files` ora sa che è un bind filesystem reale, non un errore, e che
  si legge/scrive con gli stessi verbi di ogni altro file del topic. Il
  verbo per agganciarla/sganciarla (`topic.local_folder_add`/`remove`)
  resta riservato a `clodia`/`sysadmin`, come la whitelist globale — non è
  nel pavimento di archseed: sapere che esiste non è la stessa cosa di
  poterla spostare.

## [7.22.0] — 2026-09-18
- **`clodia` e `ophelia` guadagnano lettura/scrittura Drive** (clodia-platform#378):
  `gdrive.list`/`search`/`mkdir`/`upload`/`download`/`rename`/`move`, enumerati
  uno per uno (niente wildcard, coerente col vincolo già su `clodia`).
  `gdrive.share` resta escluso per entrambi: è uscita verso una PERSONA
  (condivisione con un'email esterna), stessa classe di `email.reply`.
- **`ophelia` risale a 3/3 nel calcolo `agent_profile`** (capacità pura per-seed,
  MAI mostrata come badge — solo `context_profile`/`_channel_trifecta` a
  livello di canale conta per il rischio reale, ed è basato su fatti: file
  davvero presenti, contaminazione realmente avvenuta, destinazione realmente
  non confinata). `gdrive.upload`/`mkdir`/`move` sono egress nel catalogo
  `trifecta.yaml`. 3 test aggiornati.

## [7.21.0] — 2026-09-17
- **Egress/ingress: due assi, due platee** (clodia-platform#374). Segnalato da
  Davide su `SEAL-1/hedge-iot-new`: `sysadmin` ha aperto un `egress.allow`
  GLOBALE dove serviva uno scopato al topic, poi ha negato (erroneamente) che
  esistesse un meccanismo scopato — confondendolo col campo non correlato
  `drive_folders`. Il meccanismo scopato esiste già ed è generico
  (`egress.py::scope_allow`/`scope_uris`, verbi `topic.egress_add`/
  `ingress_add`, GATE_WALLS da R17/#334): il gap era che **nessun seed li
  dichiarava**, quindi nessun bot poteva usarli.
- **`topic.egress_add`/`egress_remove`/`ingress_add`/`ingress_remove` nel
  pavimento** (`archseed`): ogni bot li eredita ora, gated WALLS — solo
  l'owner della stanza in cui gira lo spawn approva. Direttiva di Davide:
  «locali al canale, aggiungibili da qualunque bot, sempre tramite gate».
- **`egress.allow`/`ingress.allow` (i GLOBALI) aggiunti a `clodia`**, accanto a
  `sysadmin` che già li aveva: «globali, gestiti solo da clodia e sysadmin» —
  prima la coordinatrice ne era esclusa.
- `test_192_decisioni_del_coordinatore.py::PERIMETRO` esteso: la nuova
  decisione (17 set) è più larga della #192 (6 set) sul segretario — non tocca
  chi è nella stanza (resta vietato), solo quali destinazioni/fonti di rete
  sono vagliate.

## [7.20.0] — 2026-09-16
- **Nuovo principio 7 in `platform-core.md`: sinteticità.** Richiesta diretta
  di Davide — le risposte degli agenti, in particolare quelli su provider
  Anthropic, tendono a essere verbose oltre il necessario. Il principio chiede
  la minima estensione che basta a essere corretto e completo (niente
  premesse, niente alternative scartate non richieste), senza sacrificare il
  rigore sulle domande tecniche: l'approfondimento resta disponibile ma solo
  su richiesta esplicita dell'interlocutore. Numerato **7** (ultima priorità):
  cede sempre ai principi 1-6, in particolare al 5 (igiene dell'output), da
  cui è distinto — il 5 vieta di esporre il ragionamento, il 7 regola la
  lunghezza della risposta stessa. `platform-core` è referenziato da
  `constitution:` in quasi tutti i seed non minimali del catalogo (base-pack +
  business-pack + it-pack + studio-legale + studio-commercialista): la
  modifica si propaga automaticamente al prossimo redeploy, senza toccare i
  singoli `agent.yaml`.

## [7.19.0] — 2026-09-16
- **`messaggero` passa a `glm-5.2`** (era `gpt-oss-120b`), su richiesta
  dell'owner — «upgrade del seed messaggero ad un modello più potente»
  (clodia-platform#370). Provider invariato: `scaleway` dichiara già `glm*` nel
  suo catalogo, quindi nessun cambio di sovranità (SEAL-3) e nessun tocco alla
  catena di fallback (`aws-region-eu` → `claude-haiku-4-5`). Il guadagno
  misurabile è la finestra: **1M contro 128k**, cioè il thread email lungo che
  prima non ci stava.
- **Non `glm-5.3`**, e la ragione è verificabile invece che prudenziale: nel
  motore non esiste alcun riferimento a quel modello e `model_context` non ne
  mappa la finestra — il glob `glm*` di Scaleway lo accetterebbe comunque e la
  stanza mostrerebbe **200k**, il fallback di famiglia, come se fosse un dato.
  Un numero sbagliato è un difetto peggiore di una versione in meno, perché non
  si vede. Riaprire la scelta richiede prima di interrogare il catalogo
  Scaleway, che da qui non è raggiungibile.
- **`reasoning_effort: none` dichiarato nel seed.** Il runtime lo applicava già
  di suo (`_opencode_reasoning_effort`: il reasoning di glm-5.2 non converge
  sugli esecutori di tool, iterano finché scade il read timeout e la sessione
  sembra piantata), ma quel ramo è una rete per i pack importati vecchi, non una
  decisione di questo seed. Un default può cambiare; una riga nel file no.
- Precedente di forma: **6.4.2**, lo swap di modello del `segretario`.
- Copertura: `EngineDeclarationTests` in `server/agents/test_base_pack_seeds.py`
  — il modello, il reasoning esplicito, e la finestra pinnata **al numero**
  (1M), così chi cambia modello domani deve tornare su `model_context` e
  dichiarare la finestra vera invece di lasciarla ripiegare. Il quarto test non
  riguarda il messaggero: per **ogni** seed del pack verifica che almeno uno dei
  `providers` dichiarati serva davvero il modello (riusando
  `provider_supports_model`, non una seconda copia del glob) — è il controllo
  che mancava quando un modello non risolto si scopriva al primo turno.
- **Nota per chi legge il ticket originale**: la #370 nomina tre skill del
  messaggero, il seed ne dichiara **due** (`comms-pack/check-email`,
  `comms-pack/telegram-1to1`). `mention-relay` non è stata dimenticata: è
  decaduta col meccanismo A (clodia-platform#360/#361).

## [7.18.0] — 2026-09-12
- **`sysadmin` non promette più il lifecycle dei run.** Il seed elencava
  «**Workflow** (`workflows.*`): osservi + lifecycle run» fra i namespace
  operativi e `/workflows` nella mappa della WebUI: verbi e rotta non esistono
  dal 9 ago 2026 (engine rimosso, cfr. 7.6.0). Un mandato che l'agente rilegge
  a ogni turno e non può eseguire è peggio di un mandato mancante — ci prova,
  poi spiega all'utente perché non riesce. Rimossi anche dalla `description`.

## [7.17.0] — 2026-09-13
- `messaggero`: il mandato distingue l'ordine di **leggere** dal mandato di
  **spedire** (clodia-logic#415). Nuova sottosezione «Leggere non è rispondere»
  dentro «Policy outbound (rigida)», scritta sull'incidente dell'11 set 2026
  (SEAL-2 `titul-brightnode`): chiesto di leggere un'email che sollecitava
  conferme contrattuali, l'agente ha risposto al mittente impegnando lo studio,
  senza che il testo fosse mai stato mostrato né approvato.
- Tre vincoli, e il terzo è il punto: un ordine di leggere/controllare non
  autorizza a rispondere o confermare; il testo spedito è `verbatim` e, se lo
  compone l'agente, va mostrato in chat PRIMA dell'invio; **l'approvazione sulla
  destinazione non copre il contenuto**. Il `verbatim` esisteva già nel mandato
  ma solo per Telegram — cioè non sul canale dove l'incidente è avvenuto.
- Il vincolo sta nel system prompt e non nella `MEMORY.md` dell'istanza (che è
  per-agente, non verificabile e non sopravvive a un Update), né in una rule:
  `gated_tools`/`gated_in_channel` del seed sono vuoti per decisione dell'owner
  del 7 ago 2026 — il presidio è sulla destinazione «finché la catena `origin`
  non è in enforcement». Sul contenuto, oggi, il testo del mandato è l'unica
  barriera, e deve stare dove il modello lo legge sempre.
- Copertura: `server/agents/test_messaggero_read_is_not_reply.py`, con le
  asserzioni ritagliate sulla sola sezione «Policy outbound» — sul file intero
  sarebbero state verdi prima del fix.

## [7.16.0] — 2026-09-12
- `editorial-pack` rimosso da questo repo: fuso in `business-pack`
  (clodia-packs), che consolida anche il precedente `media-agency-pack` e
  aggiunge tre seed derivati (`articolista`, `titolista`, `fact-checker` da
  `content-creator`/standalone) più `sales-rep` (lead-gen/outreach). Non è
  più bundlato nell'immagine: da questa versione richiede un import esplicito
  del pack, come qualunque altro pack di dominio.
- `clodia`/`ophelia`: `editorial-pack/*` sostituito da
  `business-pack/article-spec`, `business-pack/editorial-review`,
  `business-pack/fact-check` — stesse tre skill di prima, minimo cambiamento
  per non introdurre una decisione di scope non richiesta (se convenga
  delegare fact-check/editorial-review al nuovo seed `fact-checker` invece di
  tenerle è una domanda aperta, non risolta qui).

## [7.15.2] — 2026-09-12
- Added `SETUP.md`: missing even upstream, not just on installed instances
  (clodia-platform#339). Trivial by construction — no `requires`, no MCP
  server, no `rag_collections` — but the gap was real: `sysadmin`'s setup
  protocol reads `SETUP.md` as its runbook, and a pack silently exempt from
  having one is indistinguishable from a pack nobody documented.
- Depends on the matching `clodia-logic` server fix (`install_pack_from_root`
  now copies `SETUP.md`/`CHANGELOG.md` from the pack root — it never did,
  for any pack): without it this file would sit in the source and never
  reach a datadir, on install or on Update.

## [7.15.1] — 2026-09-11
- Fix: 7.15.0 declared the `contacts` datastore in `pack.yaml` but forgot the
  actual source `install_plugin_from_root` reads, `plugins/base-pack/
  .claude-plugin/plugin.json` — still `7.0.0`, no `datastores`. Clicking
  Update on an installed instance therefore re-wrote `pack.yaml` (already
  correct) and left the enforced manifest, `plugins/base-pack/plugin.yaml`,
  untouched: `contacts` stayed invisible to `datastore.read`/`write` even
  after Update. Measured on `personal`, 11 Sep 2026. `plugin.json` now
  carries the same `datastores` block; bumped to `7.1.0` (its own, separate
  version track from the pack's).

## [7.15.0] — 2026-09-10
- New datastore `contacts`, detached from the `tomato` pack: a contacts CRM
  is a platform resource, not a company one. Declared with `clearance:
  SEAL-1` and `seeds: [messaggero, clodia]` — the new access-control schema
  on datastore manifests (clodia-logic `_sanitize_datastores`), enforced by
  the new gateway verbs `datastore.read`/`datastore.write` (clodia-tools
  2.13.0, same two-axis model as topic access: clearance AND an explicit
  allowlist). `leads` stays in `tomato`, ungated for now — Davide asked for
  `contacts` specifically.
- `skill_sync._datastore_map` now resolves `<DATASTORE:key>` tokens across
  ALL installed packs, not just the skill's own: `osint-lead`/
  `linkedin-reactions` (still in `tomato`) reference `<DATASTORE:contacts>`,
  which now lives here. Own pack still wins on a name collision.

## [7.14.0] — 2026-09-10
- `clodia` gains `web.download` (clodia-tools 2.12.0): a colony agent hit a real
  wall trying to read a PDF — `web.fetch` refuses non-text content-types on
  purpose (the body would decode to replacement characters, no value to a
  model), and nothing else could write a binary from an external URL. This
  verb is the binary twin: bytes land on the agent's scratch, never in the
  tool-call response, same pattern as `gdrive.download`. PDF/PNG/JPEG/GIF/WebP
  only, 25 MB cap.

## [7.13.0] — 2026-09-07
- **A report is not a summons** (clodia-logic#336). The
  `multiagent-collaboration` skill promised «N tags → N agents activated (in
  parallel)» while the runtime, with the fan-out off (the default), starts
  **neither** of two mentions and opens a disambiguation question instead. Two
  texts that contradict the runtime teach the defect rather than the rule: the
  skill now states the one-mention-per-message rule and what a `@` costs (a full
  turn of someone else's context).
- The counterexample the issue handed us, now written down: when you **narrate**
  something that involves another agent — «`$sysadmin` opened the issue»,
  «`$fullstack-dev` was tagged yesterday» — the seal is `$`. A `@` inside a
  sentence of reported speech summons for real, and next to an actual request in
  the same message it makes two mentions, i.e. zero turns.
- The stale promise about a soft mention is gone from here too: `$` opens no
  turn, so there is no «answer only if you have something useful» to instruct
  (R12) — the citation is read from channel history at the next natural turn.

## [7.12.0] — 2026-09-06
- **The secretary's mandate learns to be convened** (agents-notebook A5,
  clodia-platform#196). The issue asked for «the two verbs to convene a team».
  Measured in repo, half of it was already there and the other half was not
  needed: `topic.suggest_team` is in the seed since 7.x, and the fourth summon
  exists in code — `_record_fallback` picks the coordinator from `ai_all`, i.e.
  **before** the `state_writer_only` filter, so in a room where Clodia's provider
  does not cover the tier the secretary already receives `[COORDINAMENTO]`.
- **The defect was in the text, not in the verbs.** The mandate said «if you get
  an out-of-domain request, answer only: *out of domain, ask the captain*» —
  with no exception. Summoned **as** the captain, it answered «ask the captain».
  The `topic-state-boundary` rule had carried the exception since it was written;
  the seed prompt still said the opposite, and both are in the same context.
- The coordination section now spells out **four outcomes**: it is topic-state
  work → do it; it belongs to another participant → hand it over with one
  `@name`; **nobody in the room fits but the colony has someone** → propose the
  squad with `topic.suggest_team` and close with `<!-- invite=… -->`; nobody
  anywhere → say so, and name the remaining remedy. The third is the point of the
  issue: a refusal is the right answer when there is someone else to ask, and the
  useful answer when there is not is *who would be needed*.
- **`topic.add_participant` is not granted**, and the choice is recorded: the
  skill closes with the invite marker and the owner clicks the button, so the
  verb would add authority without adding capability — and it would reopen
  clodia-platform#104 §10.2 («take `add_participant` from everyone but clodia»),
  whose test names the secretary by name.
- **Two tools the gate denies leave the mandate.** «Cosa fai» still ordered
  `topic.add_minute` and `topic.write_file`, removed on 5 Aug 2026 (7.1.0,
  clodia-platform#212). Minutes are a section of the summary, saved with the one
  verb the seed has. Same class of defect in `sysadmin`, found by the test that
  guards this one: its prompt named `topic.list_files` and `topic.put_file`,
  which are not verbs at all — the real ones are `topic.files` and `topic.put`.
- The guard is written over **every** seed of the pack, not over the secretary
  alone: both cases were born the same way — a revocation that edited
  `agent.yaml` and forgot the `system-prompt.md` next to it. With the
  declaration correct the seed looks fine and the agent still reaches for a tool
  that will be refused.

## [7.11.0] — 2026-08-17
- **`comms-pack/*` removed from `clodia`'s capabilities** (agents-notebook A7,
  clodia-platform#198): the post is the **courier's** trade. Profile measured in
  repo: base 5 + editorial 3 + comms 4 = **12 skills → 8**.
- Nothing is orphaned. The four skills — `check-email`, `mention-relay`,
  `telegram-1to1`, `helpdesk` — stay declared **one by one** on `messaggero` and
  `sysadmin`, which hold them by role; `clodia`'s system prompt never names any
  of them. And `email.send` is a **verb**, not a pack skill, so the daily report
  still goes out (7.9.0 put it back deliberately).
- The test that guarded this requirement — in
  `server/agents/test_base_pack_seeds.py` — was born **red**, with
  `@unittest.expectedFailure` and the issue number beside it (decision record
  34). The marker is gone and the test now
  guards the requirement instead of documenting its absence: the wildcard would
  come back on the first hand edit of the seed, and without that line it would
  come back in silence, exactly as it stayed for nine days.
- **`ophelia` deliberately untouched.** Its `capabilities` carry the identical
  `comms-pack/*` wildcard, but A7 names only `clodia`; aligning a seed the issue
  does not name is a scope decision of its own, tracked separately.
  `anthropic-pack/*` likewise stays a wildcard: it resolves from `datadir/skills-catalog`,
  so its skills are not in this repo and cannot be weighed from here.
- **Operational step required after merge**, not a detail: `seed_sync.sync_seeds()`
  skips directories that already exist (`if target.exists(): continue`) and
  `backfill_new_fields()` only fills **absent** fields from a closed list
  (`native_tools`, `denied_tools`, `all_tier`) — `capabilities` is not on it. So
  the live `/datadir/agents/clodia/agent.yaml` keeps the four skills until
  someone edits it by hand. Same class of gap as clodia-platform#220: a fix to
  the pack does not reach the running instance on its own.

## [7.10.0] — 2026-08-15
- **`rules: ["*"]` removed from `clodia` and `ophelia`.** The catalogue holds
  exactly one rule — `topic-state-boundary` — and it belongs to the secretary:
  it confines whoever maintains a topic's written state, declares "using tools
  other than the topic-state ones" out of domain, and prescribes "a short,
  operational refusal". Under the wildcard both agents inherited it, so the
  wildcard effectively said *behave like the secretary*.
- Measured: the `Daily digest GRC` job closed `success` for four mornings while
  replying "non rientra nel mio ambito: il mio ruolo qui è mantenere lo stato
  scritto del topic" — the rule's own words, from an agent whose job was to read
  the web and mail a report.
- Same defect as the tool wildcard retired on 6 Aug, seen from the other side:
  there it silently granted every power added tomorrow, here every LIMIT written
  for someone else's trade. Rules get declared one by one, like verbs.

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
