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
`messaggero`+`clodia` — vedi `pack.yaml`). Non richiede provisioning: il file
arriva con l'import/update stesso. Verifica dopo l'Update:

- `datastore.read("base-pack/contacts", "SELECT count(*) FROM contacts")` come
  seed `clodia` o `messaggero` risponde (non "datastore non trovato").
- Se risponde "non trovato" nonostante il pack sia alla versione che dichiara
  `datastores:`, il `plugin.yaml` importato è rimasto a una versione
  precedente: rilancia Update (clodia-platform#337 per un caso già visto).

## 5. Verifica finale

- Gli agent seed del pack (`clodia`, `ophelia`, `messaggero`, `sysadmin`,
  `segretario`) sono caricati (`agents.list`).
- `topic.*` risponde per i seed che lo dichiarano nel proprio profilo.
- Nessun'azione manuale ulteriore è prevista: un setup che ne richiedesse una
  sarebbe un segnale che il pack ha smesso di essere "banale" e questo file va
  aggiornato di conseguenza (clodia-platform#339).
