# Pack e Plugin — gerarchia del catalogo

Due livelli (decisione 4 lug 2026):

```
pack   := [agent seeds] + [plugins]        # unità di distribuzione Clodia
plugin := [skills] + [rules] + [mcp]       # standard Claude Code
```

**Nessun componente è mai obbligatorio.** Un plugin può essere una singola
skill; un pack può contenere solo un seed, solo plugin, o qualunque
combinazione. I plugin possono vivere anche **sciolti**, fuori da qualunque
pack. La webui (pagina Packs) naviga il catalogo come tree:
pack → (agents | plugins) → skills / rules / MCP server.

## Plugin

| Componente | Path |
|---|---|
| skills | `CLODIA_DATA/skills-catalog/<plugin>/<skill>/SKILL.md` |
| rules | `CLODIA_DATA/rules-catalog/<plugin>/<rule>.md` |
| manifest (metadata + mcp_servers) | `CLODIA_DATA/plugins/<plugin>/plugin.yaml` |

Plugin impliciti (non importabili/rimovibili): **`base-pack`** (catalogo logic
in git) e **`local-pack`** (entry flat del data catalog). I nomi storici
(`anthropic-pack`, `user-pack`, …) restano invariati: sono etichette, l'entità
è il plugin.

Origini (`origin`): `logic`, `local`, `external` (da `external-packs.yaml` al
setup), `user`, `imported`. Cancellabili: external / user / imported.

Formati riconosciuti da `POST /clodia/plugins/import[-url]`:

1. **Claude plugin** — `.claude-plugin/plugin.json` (+ `skills/`, `.mcp.json`)
2. **Clodia plugin** — `plugin.yaml` (legacy `pack.yaml` v6.57) + skills/rules/mcp
3. **Bare skills** — nessun manifest → `user-pack`

## Pack

Formato di un pack (repo `clodia-packs` = directory di pack):

```
<pack>/
├── pack.yaml               # name, description, version
├── agents/<seed>/          # agent.yaml + system-prompt.md + memory/ (+ pfp.png)
└── plugins/<plugin>/       # ciascuno un plugin (plugin.json/plugin.yaml o bare)
```

Manifest runtime: `CLODIA_DATA/packs/<pack>/pack.yaml` (name, description,
version, source, agents, plugins).

**Import** (`POST /clodia/packs/import[-url]`, unificato): se l'archivio è un
pack installa plugin e seed; altrimenti delega all'import plugin
(`kind: "pack" | "plugin"` nella risposta).

**Install dei seed**: l'agente viene installato E registrato — copia in
`CLODIA_DATA/agents/<name>/`, emissione cert PKI (senza cert l'agente non si
autentica al gateway e vede zero tool), `registry.load()`, whitelist sul
gateway. PKI e whitelist sono best-effort (l'entrypoint fa `issue-all` a ogni
boot). Un seed esistente NON viene sovrascritto (`status: exists`); i nomi
nativi (clodia/ophelia/mercuria) sono rifiutati.

**`requires_plugins`** (in `agent.yaml` del seed): prerequisito **soft** verso
un plugin:

```yaml
requires_plugins:
  - name: eu-project-design
    hard: false        # default; anche la forma breve "- eu-project-design"
```

Plugin mancante → l'agente parte comunque in modalità degradata; l'API packs
espone `missing_plugins` per il warning in UI. `hard: true` è dichiarativo
(nessun enforcement al boot, riservato a policy future).

**Delete** (`DELETE /clodia/packs/{name}`): rimuove i plugin del pack, i suoi
agenti non nativi e il manifest.

## MCP server dei plugin: esposti, mai auto-montati

Config esposte dal catalogo con secret mascherati; il mount sul gateway resta
un'azione esplicita dell'owner dalla sezione Tools (Prima Legge: uno zip
importato non deve attivare endpoint o processi arbitrari). I seed invece
vengono registrati all'import: sono agenti della piattaforma, non endpoint
esterni — e restano inerti finché non gli si parla o non li si schedula.

## API

- `GET /clodia/packs` · `GET /clodia/packs/{name}` — pack con agenti
  (installed, requires/missing_plugins) e plugin risolti
- `POST /clodia/packs/import` · `/import-url` — import unificato pack|plugin
- `DELETE /clodia/packs/{name}`
- `GET /clodia/plugins` · `GET /clodia/plugins/{name}` — tutti i plugin
  (anche sciolti)
- `POST /clodia/plugins/import` · `/import-url` · `DELETE /clodia/plugins/{name}`
- `/clodia/skills` e `/clodia/rules` restano invariate (item con `pack`/`variants`)

## Grant per-agent

In `agent.yaml`, skills e rules usano la grammatica plugin-aware (invariata):

```yaml
capabilities: ["base-pack/*", "eu-project-design/*"]
rules: ["secrets-handling", "acme-pack/*"]
```

`<plugin>/<nome>` = elemento qualificato; `<plugin>/*` = tutto il plugin;
`*` = tutto il catalogo (super-agent).
