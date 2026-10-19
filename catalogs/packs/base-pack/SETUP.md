# SETUP - base-pack (runbook sysadmin)

Istruzioni per il **sysadmin** dopo l'import/update del pack. `base-pack` è
first-party e trusted per costruzione (non passa dal security-review gate, non
è disinstallabile) — il setup è banale per progetto, non per trascuratezza:
niente da provisionare fuori da ciò che l'import stesso già scrive.

## 1. Dipendenze del plugin (`requires`)

Nessuna. `base-pack` non dichiara `requires.pip/npm/bin` — le primitive che
espone (`topic.*`, `agents.*`, `memory.*`, `fs.list_dir`, `github.*`,
`runtime.*`) sono verbi nativi del gateway, non processi esterni da installare.

## 2. Server MCP

Nessuno. `mcp_servers: {}` nel manifest — a differenza di pack come
`assetti-contabili` o `bandi-pack`, `base-pack` non monta un backend MCP
proprio.

## 3. Provisioning RAG

Nessuna `rag_collections` dichiarata dal pack in sé.

## 4. Datastore

Il pack dichiara un datastore, `contacts` (`data/contacts.db`, SEAL-1, seed
`clodia` — vedi `pack.yaml`). Non richiede provisioning: il file
arriva con l'import/update stesso. Verifica dopo l'Update:

- `datastore.read("base-pack/contacts", "SELECT count(*) FROM contacts")` come
  seed `clodia` risponde (non "datastore non trovato").
- Se risponde "non trovato" nonostante il pack sia alla versione che dichiara
  `datastores:`, il `plugin.yaml` importato è rimasto a una versione
  precedente: rilancia Update (clodia-platform#337 per un caso già visto).

## 5. Verifica finale

- Gli agent seed del pack (`clodia`, `ophelia`, `sysadmin`,
  `segretario`) sono caricati (`agents.list`).
- `topic.*` risponde per i seed che lo dichiarano nel proprio profilo.
- Nessun'azione manuale ulteriore è prevista: un setup che ne richiedesse una
  sarebbe un segnale che il pack ha smesso di essere "banale" e questo file va
  aggiornato di conseguenza (clodia-platform#339).

## 7.40.0 — messaggero retired (clodia-platform#531)

The `messaggero` seed is gone. Inbound mail reaches the topics through the
platform's deterministic courier (clodia-logic ≥ 6.311.8, clodia-tools ≥
2.122.1, comms-pack ≥ 2.3.1); outbound messages use the gateway verb
`channel.deliver` (clodia-tools ≥ 2.123.0), granted to `clodia`. It is not
granted to `segretario`, whose read-only perimeter (#192) would gain the egress
leg of the trifecta: an owner who wants it there grants it explicitly. After the Update an instance still has the old `messaggero` agent
installed: remove it from the Agents page (or `DELETE /api/agents/messaggero`)
and revoke its vault grants; the Telegram relay no longer needs it.
