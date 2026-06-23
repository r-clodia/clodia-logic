# AGENT-SERVER POLICY

**Versione**: 5.11.0
**Documento Normativo**

Regole operative del tool `agent-server` — daemon HTTP locale che espone una singola sessione Clodia in GUI web + CLI.

---

## 1. Identità e Scopo

`agent-server` è un daemon HTTP locale che:
- Mantiene una **singola sessione long-running di Clodia** (workspace `/Users/erreclaudea/erre-claudia`)
- Espone API REST + SSE per chat e controllo
- Funge da single source of truth: GUI web + CLI (`clodia`) sono client thin

L'utente principale è l'owner dell'istanza (chi ha fatto il bootstrap admin). Da 1.0-rc è stato eliminato il sistema multi-agent-type; tutto il valore è concentrato sulla chat con Clodia.

---

## 2. Architettura

```
agent-server/
├── server/
│   ├── main.py            # FastAPI app + lifecycle Clodia
│   ├── api/
│   │   ├── agents.py      # endpoint /clodia/* (chat, history, events, interrupt, status)
│   │   ├── daemons.py     # endpoint /daemons (gestione demoni esterni)
│   │   └── health.py      # /health (version + commit)
│   ├── core/
│   │   ├── events.py      # EventBus pub/sub per SSE
│   │   └── models.py      # ClodiaStatus enum, Event, ChatMessage, DaemonInfo
│   ├── sdk_runtime/
│   │   └── session.py     # ClodiaSession singleton (claude-agent-sdk wrapper)
│   └── storage/
│       └── daemons.py     # registry dei demoni esterni
├── cli.py                 # client CLI (server start/stop/status, chat, history, follow)
├── sessions/              # JSONL history (clodia-session.jsonl)
└── frontend/              # SvelteKit (single chat + sidebar demoni + footer versione)
```

Endpoint principali:
- `POST /clodia/messages` — invia un turno utente, restituisce la risposta completa
- `POST /clodia/interrupt` — cancella il turno corrente (sessione resta viva)
- `GET /clodia/history` — history JSONL completa della sessione
- `GET /clodia/events` — stream SSE (status, message, message_chunk, usage, error, interrupted)
- `GET /clodia` — status corrente
- `GET /health` — version + commit short SHA
- `GET /daemons` + `POST /daemons/{name}/start|stop` — gestione demoni
- `GET /files/download` + `GET /files/check` — download file con whitelist di root
- `POST /files/upload` — upload (multipart) di file droppati dalla GUI verso `/Users/Shared/`. Sanitizza il nome, evita collisioni con suffisso numerico, limite 100 MB
- `GET /topics` — lista topic attivi
- `GET /topics/{cls}/{name}/summary` — contenuto raw di summary.md
- `GET /topics/{cls}/{name}/tree` — struttura ad albero del topic (summary + files/, ricorsivo)
- `GET /topics/{cls}/{name}/file?path=...` — contenuto raw di un file dentro il topic (read-only, whitelist di estensioni testuali, cap 5 MB, anti path-traversal)

---

## 3. Sessione Clodia

- **Singleton** lato server: una sola istanza di `ClodiaSession`, avviata al boot in background
- **Workspace**: cwd = `/Users/erreclaudea/erre-claudia` → il binary `claude` legge automaticamente `CLAUDE.md`, `MEMORY`, settings utente (incluso UserPromptSubmit hook RAG), keychain (OAuth Max)
- **Niente system_prompt override**: usa quello che il binary `claude` carica naturalmente
- **History persistita** in `sessions/clodia-session.jsonl` (append-only)
- **Lock asincrono** per serializzare i turni concorrenti

Watchdog `COLLECT_TIMEOUT_SECONDS = 10 * 60`: se il loop `receive_response` del SDK non termina entro 10 min, scatta `TimeoutError` e lo status passa a ERROR.

Interrupt manuale via `POST /clodia/interrupt`: cancella il task del turno corrente (status → CANCELLING → IDLE), lascia la sessione viva.

---

## 4. Autenticazione

Il subprocess `claude` lanciato dal SDK può usare **OAuth (Claude Pro/Max)** via keystore dell'istanza oppure `ANTHROPIC_API_KEY` (pay-per-token), a seconda del provider configurato dall'owner.

---

## 5. Sicurezza

- **Credenziali**: nessuna in code o config. Il keychain è di sistema, gestito da macOS.
- **Logging**: i logger di librerie HTTP (`httpx`, `httpcore`) sono silenziati a WARNING nei daemon (vedi `trello-poller`) per evitare leak di URL con query params autenticati. L'agent-server in sé non fa chiamate autenticate dirette.
- **Bind**: `127.0.0.1:7842` (loopback only).
- **No remote access**: il server non espone porte di rete pubbliche.

Il subprocess `claude` figlio del server ha pieno accesso al workspace `/Users/erreclaudea/erre-claudia` perché è Clodia. Per qualsiasi limitazione operativa (paths, tool, shell), si configurano direttamente CLAUDE.md e settings di Claude Code.

---

## 6. Frontend

SvelteKit static build, servito da FastAPI come SPA. UI:
- Sidebar sinistra: stato Clodia (dot + status), elenco demoni con start/stop, footer versione + commit
- Main: header con cwd, area messaggi, textarea + bottone Invia (diventa "Stop" durante thinking, hotkey Esc), token counter
- SSE per aggiornamenti realtime di status, message_chunk, usage

---

## 7. Versionamento

Versione corrente: **1.1-rc** (release candidate). Estensione della sidebar con esplorazione topic ad albero, viewer markdown in nuova tab, sidebar ridimensionabile.

Cambiamenti vanno bumppati seguendo semver:
- patch: bugfix
- minor: feature non-breaking (es. nuovi endpoint)
- major: breaking change API

### Storico

- **1.1-rc (frontend 0.4.0)** — Sidebar ridimensionabile (drag handle sul bordo destro, larghezza persistita in `localStorage` con clamp [200, 600]). Titolo topic in sidebar va a wrap su 2 righe (line-clamp) invece di troncamento brutale. Topic mostrato come albero espandibile: ogni topic ha `summary.md` + `files/` (ricorsivo). Click su un file foglia apre una nuova tab `/view?cls=&name=&path=` che renderizza solo markdown (no chat, no inferenza). Backend: nuovi endpoint `GET /topics/{cls}/{name}/tree` e `GET /topics/{cls}/{name}/file` (whitelist estensioni testuali, cap 5 MB, anti path-traversal stretto). Rimosso il vecchio comportamento "click topic → carica summary inline" sostituito dall'albero. Branch: `feat/sidebar-resize-topic-tree`.
- **1.0-rc (frontend 0.3.0)** — Drag&drop di file nella finestra di chat (card Trello [qDiRVECd](https://trello.com/c/qDiRVECd)). Backend: `POST /files/upload` (multipart) salva in `/Users/Shared/` con nome sanitizzato, anti-collisione (`-1`, `-2`, …), limite 100 MB. Frontend: overlay drag attivo su tutta la chat, ogni file droppato viene caricato e il suo path appeso alla textarea come `[file]: /Users/Shared/<nome>` così Clodia ha contesto.
- **1.0-rc (frontend 0.2.0)** — Aggiunti bottoni "Copia" e "⬇ .md" in fondo a ogni messaggio assistant (card Trello [0VuIYTJ2](https://trello.com/c/0VuIYTJ2)). Copia: markdown grezzo negli appunti con feedback "✓ Copiato" 1.5s; download: file `clodia-<msgId>.md`. Stile discreto (border dashed, opacity 0.55 → 1 su hover del messaggio).
- **1.0-rc** — Tabula rasa. Eliminati: `agent-types/`, `agent-instances/`, `system-prompt-modules/`, codice multi-agent (`composer.py`, `registry.py`, `agent_types.py` endpoint, `instances.py` storage, `reviews.py` storage, sistema warm pool multi-type, sistema review per type). Backend ridotto a singleton `ClodiaSession`. Frontend ridotto a single chat + demoni. CLI ridotto a `server start/stop/status`, `chat`, `status`, `history`, `follow`. Backup pre-refactor in `dump/agent-server-pre-tabularasa-20260521/`.
- **0.6.3** — orchestrator slim prompt + MCP whitelist completa.
- **0.6.2** — patch prompt no-preamble per orchestrator e ada.
- **0.6.1** — patch: spinner UI single source of truth backend + topic recap disabilitato + footer version/commit.
- **0.6.0** — warm pool multi-type con keep-alive 4 min.
- **0.5.0** — auth via OAuth Max (keychain) invece di API key.
- **0.4.0** — interrupt manuale + watchdog timeout.
- **0.3.0** — review conversazionale `.vote`, token counter.
- **0.2.0** — review system base.
- **0.1.0** — first release.

---

## Colonia di Agenti — Colony Agent Platform (CAP, v5.0.0)

Da v5.0.0 l'agent-server implementa la spec "Colony Agent Platform v0.1"
(giu 2026). Documento architetturale completo: `COLONY.md`.

Regole operative CAP:

1. **Trello non è il database** (spec §21): lo stato autorevole di
   pipeline, executions, claims, heartbeats, deliverables ed eventi vive
   in `CLODIA_DATA/data/colony.db` (SQLite WAL; override `COLONY_DB_URL`
   per PostgreSQL). Gli state file JSON restano cache di compatibilità.
2. **Pipeline esplicite**: l'esecuzione passa SEMPRE da una pipeline
   registrata (`pipes.yaml` + registry) con stati formali
   DRAFT→VALIDATING→READY→ACTIVE⇄PAUSED→DEPRECATED. L'attivazione
   provisiona la board Trello (board=pipeline, lane=step, card=task).
3. **Strategy Agent** (spec §4.2): produce solo `strategy_output` JSON;
   non può attivare pipeline, modificare board o credenziali. Generazione,
   validazione e attivazione passano da Clodia/owner via API.
4. **Heartbeat e recovery** (spec §14-15): heartbeat ogni 30s, timeout
   180s → STALE → recovery con nuovo execution_id (`retry_of`). Retry
   limitati dalla `retry_policy` della skill; esauriti → ESCALATED.
5. **Deliver strutturato** (spec §17): ogni worker scrive
   `scratch/outcome.json` (status, result_type, deliverables,
   side_effects). Side-effect sensibili (deploy, email, pagamenti,
   cancellazioni, credenziali, pubblicazioni) → ESCALATED + notifica
   Telegram a owner (approval gate, spec §22).
6. **Workspace effimeri** (spec §13, §16): `executions/<execution_id>`,
   cleanup immediato solo a DELIVERED, retention 24h per i falliti.
7. **Credenziali** (spec §19): dedicate per agente in
   `secrets/agents/<name>/`, fallback alle globali con warning in audit.
   Mai esposte al motore di inferenza.
8. **Audit** (spec §20): ogni operazione → riga in `events`
   (queryabile via `GET /api/colony/events`).

## Keystore MCP + PKI della colonia (v5.1.0)

Da v5.1.0 le credenziali degli agenti passano dal **keystore**: server MCP
unico via HTTP (`/keystore/mcp`), autenticato con la PKI della colonia.

1. **Clodia è la CA** (`secrets/ca/`): ogni agente riceve al seed una
   coppia ed25519 + certificato X.509 firmato dalla CA (`pki/certs/`,
   pubblico; chiave privata in `secrets/agents/<nome>/identity.key`, area
   runner, MAI montata nel workspace). CLI: `python3 -m server.colony.pki`.
2. **Sessioni**: a ogni spawn il runner firma un token effimero
   (`ckt1.*`, TTL 45 min, legato all'execution) — è l'unica cosa che
   entra nel workspace. Revoca certificato = lockout immediato.
3. **Tool**: `keystore_whoami`, `keystore_list` (solo NOMI),
   `keystore_lease` (materializza in file 0600 nello scratch, ritorna il
   path), `keystore_release`, `keystore_git_push` (BROKER: il push lo
   esegue il keystore con la credenziale dell'agente; modi `branch` |
   `ff_main` con rifiuto se non fast-forward).
4. **Prima Legge**: il keystore non ritorna MAI il valore di un segreto
   (tutto ciò che un tool ritorna entra nel contesto del modello). Ogni
   lease/deny/push è auditato in `colony.events`.
5. Le credenziali per-agente vivono in `secrets/agents/<nome>/` e sono
   dichiarate in `agent.yaml.credentials`. Nessun fallback globale per i
   worker via keystore.

## Kanban daemon come processo separato (v5.3.0)

Dal refactor F5 il kanban daemon (skill-driven consumer) gira come
**processo dedicato** (`daemons/kanban/`, modulo
`server.agents.kanban_daemon`), non più come task asyncio dentro
l'agent-server:

- event loop isolato (un poll lento non blocca API/UI), log proprio,
  crash isolato, lifecycle uniforme (start/stop/status come gli altri
  daemon, controllabile da webui /daemons)
- contratto REST invariato: `/api/skill-consumer/start|stop|status`
  delegano agli script del daemon; `poll-now` → 410 (poll automatico 30s)
- stato condiviso col server via colony.db (SQLite WAL multi-processo),
  state file e activity log JSONL
- **limite noto**: la pausa di un agente dal server ha effetto al poll
  successivo (skip claim); per interrompere un run in corso → stop daemon
- default invariato: STOPPED al boot, avvio esplicito

### Keystore v2 — depositario unico (v5.4.0)

Le credenziali verso servizi terzi vivono in **un solo posto**
(`CLODIA_DATA/secrets/keystore/`) e sono governate da
`CLODIA_DATA/keystore-policy.yaml`: grant per agente con azioni
(`git_push` broker | `lease`) e vincoli repo. Default **DENY**. Niente
copie per-agente: l'attribuzione di ogni uso è garantita dall'identità
PKI nell'audit (`colony.events`), non dal provider. In
`secrets/agents/<nome>/` resta SOLO `identity.key` (PKI).
