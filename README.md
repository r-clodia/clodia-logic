# clodia-logic

Il **substrato** di Clodia Agency: pure logica, immutabile, versionata. È il build context dell'immagine container e il "patrimonio genetico" trasmesso ai cloni dell'agenzia.

## Cosa contiene (solo logica)
- `CLAUDE.md` — costituzione / system prompt.
- `tools/system/`, `tools/app/` — runtime agent-server + tutti i tool.
- `docker/`, `docker-compose.yml` — build e deploy.
- `daemons/` — definizioni daemon (lo *stato* va in `clodia-data`).
- *(Nessun file-template per i nuovi agent: il flusso "crea agente" genera lo scaffold direttamente dallo schema `AgentSpec` in `api/agent_registry.create_agent`.)*
- `catalogs/agents-seed/{clodia,ophelia,helpdesk}/` — seed canonici istanziati in `clodia-data/agents/` al bootstrap. `clodia` e `ophelia` sono super-agent (`constitution: platform-core`); `helpdesk` è l'agent normal dedicato al widget di assistenza in-app. Eventuali agent aggiuntivi dell'istanza vivono solo in `clodia-data/agents/`, non nel repo.

## Agent SDK
Gli agenti task-bound sono definiti in modo agnostico in `agent.yaml` tramite
`agent_sdk` (`claude`, `codex`, `opencode`) + `model`. Il workspace effimero
converte poi skill, rules e sandbox nel layout richiesto dal runtime agentico
selezionato.
Per agenti legacy già presenti in datadir: `python3 -m server.agents.migrate_agent_sdk`.

## Cosa NON contiene
Nessun dato: `boot/`, `topics/`, `secrets/`, `data/`, `contacts.db`, gli agenti assunti e la loro memoria vivono in **`clodia-data`** (volume montato a runtime).

## Governance
`main` è protetto (require PR + review). Gli agenti **possono** modificare questo repo via fork+PR: token Clodia ha `Contents:read` su upstream e `Contents:write` sul fork `clodiaolivau-r/clodia-logic`, le board "Agent Server Dev" e "Web Dev" sono nate proprio per task di sviluppo sul substrato. Il merge resta gate umano (review di owner). Niente push diretto su upstream/main, niente skip-CI, niente force-push su branch già reviewed.

## Runtime
`cwd = /clodia` (questo repo). I dati di `clodia-data` sono bind-montati in `/clodia` ai path attesi (`topics/`, `secrets/`, `boot/`, `data/`) + in `/datadir` (`agents/`, `agent-workspaces/`, `agent-state/`).
