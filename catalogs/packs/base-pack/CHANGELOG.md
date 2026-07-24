# Changelog — base-pack

Changelog del pack `base-pack` (formato [Keep a Changelog](https://keepachangelog.com/),
SemVer). La versione **in corso** è in cima. Vedi `pack.yaml` per la versione corrente.

## [6.8.0] — 2026-07-24
- **Skill `multiagent-collaboration`**: codifica il gioco di squadra nei canali —
  lavorare per OBIETTIVI (non comandi), e se mancano tool/grant/skill cercare nel
  canale chi può aiutare (`runtime.agents`) e coinvolgerlo. Convenzione dei tag:
  `@agente` = richiesta diretta (attiva, N tag → N agenti), `$agente` = menzione soft
  (l'altro giudica se intervenire, altrimenti cenno breve). La convenzione è anche
  iniettata in ogni turno di canale dal core (channels).

## [6.7.0] — 2026-07-24
- **sysadmin — accesso ai FILE dei topic con le regole comuni.** Rovescia il
  divieto assoluto: sysadmin ora ha `topic.*` nelle tool_permissions e legge/scrive
  i file dei topic (`topic.list_files/read_file/put_file/…`) **come gli altri agent**.
  L'accesso è enforced dal gateway con il modello a due assi: **participant** del
  topic + **clearance ≥ tier**; su un topic di cui non è participant scatta il
  **gate cross-topic** (approvazione owner). Nessun raw-fs (come messaggero: il
  canale ai file sono i tool). `topic.post_message` resta prerogativa di
  super/messaggero. Charter/description/system-prompt aggiornati di conseguenza.

## [6.6.0] — 2026-07-23
- **sysadmin — contesto-topic dal widget.** Quando l'utente apre l'assistenza
  mentre sta su un topic, il widget comunica a sysadmin quale topic è (commento
  nascosto in testa al messaggio) e sysadmin può ispezionarlo con il nuovo tool
  **`runtime.inspect_topic(tier, name)`** (metadati + agenti + ultimi messaggi).
  Vincolo **clearance**: solo se la SEAL effettiva di sysadmin ≥ tier del topic —
  i confidenziali sopra la sua clearance restano invisibili (403). Rilassa il
  confine "non legge mai il contenuto dei topic" → "solo entro la clearance".
- **check-email**: il job va creato con `agent = messaggero` (te stesso), esplicito
  in `jobs.propose`; il fire gira come messaggero (usa `topic.post_message`), non
  come clodia (rinforzo skill + fix gateway: `jobs.propose` default executor = chiamante).
- **janitor**: rimosso ogni residuo (seed orfano dismesso dalla colony); il widget
  di assistenza risponde con **sysadmin**.

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
