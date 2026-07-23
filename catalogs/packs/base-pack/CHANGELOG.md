# Changelog — base-pack

Changelog del pack `base-pack` (formato [Keep a Changelog](https://keepachangelog.com/),
SemVer). La versione **in corso** è in cima. Vedi `pack.yaml` per la versione corrente.

## [6.5.1] — 2026-07-23
- **Skill `check-email`** (rimpiazza `email-reconcile`, senza ledger): messaggero,
  su richiesta, crea un **job** (intervallo T) che controlla una casella, filtra
  per subject/mittente e, al match, **inietta nel topic** con `topic.post_message`
  e una **@menzione** all'agente che deve prenderla in carico. Ogni fire = turno
  breve, niente ascolto bloccante, niente stato da mantenere.
- **messaggero**: capability `base-pack/check-email` (ex email-reconcile) +
  `jobs.propose` (per creare il job) + `topic.*` (include `topic.post_message`).

## [6.5.0] — 2026-07-23
- **Consolidamento `janitor` + `sysadmin` in un unico seed `sysadmin`** (steward
  di piattaforma). sysadmin assorbe il ruolo front-of-house di janitor: è l'agente
  del **widget di assistenza** (guida WebUI, marker `goto`, guida integrazioni) E
  esegue le **platform-ops** (a differenza di janitor NON scala: esegue, con le
  mutazioni gated). Aggiunti `app_runtime.get/list/health` + capability `helpdesk`.
  **`janitor` rimosso** dal base-pack. Default `helpdesk.agent` → `sysadmin`.
- Setup di un pack: il seed sa leggere il `SETUP.md` del pack ed eseguirne il
  provisioning (deps + MCP + `rag_collections`).

## [6.4.4] — 2026-07-23
- **Skill `email-reconcile`**: routine job-driven per riconciliare la posta in
  ARRIVO nei topic in modo deterministico e sicuro — un topic riceve solo i reply
  ai thread che ha iniziato (match per header In-Reply-To/References contro un
  ledger nella seed memory). Niente routing per contenuto/triage. Non è un ascolto
  bloccante: routine breve per turno (l'ascolto è un job periodico).
- **messaggero**: capability mirata `base-pack/email-reconcile`.

## [6.4.3] — 2026-07-23
- **sysadmin → platform-ops pieno**: `tool_permissions` estesi a `agents.*`,
  `integrations.*`, `jobs.*`, `profile.*`, `providers.*`, `runtime.*`, `settings.*`,
  `workflows.*` (oltre a `packs.*`/`fs.list_dir`/`logs.tail`). Quasi tutte le
  mutazioni restano gated (M-gate). `runtime.*` include topics/chats solo metadati
  (contenuto protetto da deny_read + clearance). Remit/description aggiornati.

## [6.4.2] — 2026-07-22
- **segretario → `gemma-4-26b-a4b-it`** (Scaleway) al posto di mistral-small-24b:
  quest'ultimo in tool_choice=auto non invocava i verbi (scriveva in prosa); gemma-4
  (agentico) chiama i tool. Aggiunto prompt perentorio "agisci con i TOOL, non con
  la chat".

## [6.4.1] — 2026-07-22
- **sysadmin: verbo `runtime.restart_agent`** — restart mirato delle sessioni vive
  di un agente col runtime impuntato (history/dati persistono). Non gated.

## [6.4.0] — 2026-07-22
- **Nuovo agente `segretario`**: verbalizzatore del topic (summary/TLDR/prossimi
  passi + minute), scrittura-stato del solo topic partecipato. Default participant.
- **Versioning/Update dei pack** dalla view Packs (Check update + Update da GitHub
  upstream; sostituzione seed/skill/mcp + restart agenti).
