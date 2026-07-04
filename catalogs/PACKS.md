# Pack — entità di primo livello del catalogo

Un **pack** = `[skills] + [rules] + [mcp_servers]`, dove **nessun componente è
obbligatorio**: un pack può contenere una singola skill, solo rule, solo MCP
server, o qualunque combinazione. Il modello è compatibile con i **plugin di
Claude Code** (in generale `[skills] + [mcpServers]`).

La webui (pagina Packs) naviga il catalogo come tree: i nodi di primo livello
sono i pack, da cui si scende a skills / rules / MCP server.

## Storage

| Componente | Path |
|---|---|
| skills | `CLODIA_DATA/skills-catalog/<pack>/<skill>/SKILL.md` |
| rules | `CLODIA_DATA/rules-catalog/<pack>/<rule>.md` |
| manifest (metadata + mcp_servers) | `CLODIA_DATA/packs/<pack>/pack.yaml` |

Pack impliciti (non hanno manifest né sono importabili/rimovibili):

- **`base-pack`** — il catalogo logic in git (`catalogs/skills/` + `catalogs/rules/`)
- **`local-pack`** — le entry FLAT del data catalog (senza pack-subdir)

Origini (`origin` nell'API): `logic` (base-pack), `local` (local-pack),
`external` (installato al setup da `external-packs.yaml`), `user` (user-pack),
`imported` (importato via zip/URL). Cancellabili: external / user / imported.

## Formati di import riconosciuti

`POST /clodia/packs/import` (.zip) e `POST /clodia/packs/import-url`
(git repo o .zip remoto) riconoscono, in ordine:

1. **Claude plugin** — `.claude-plugin/plugin.json` alla root (o un livello
   sotto). `name`/`description`/`version` dal plugin.json; skills = ogni
   cartella con `SKILL.md`; MCP = `mcpServers` in plugin.json o `.mcp.json`.
2. **Clodia pack** — `pack.yaml` alla root:

   ```yaml
   name: acme-pack
   description: Pack di dominio ACME
   version: 1.0.0
   mcp_servers:            # formato mcpServers di Claude Code
     kb:
       type: http
       url: https://kb.example.com/mcp/
       headers: { Authorization: "${KB_TOKEN}" }
   ```

   Skills = cartelle con `SKILL.md`; rules = `rules/*.md`.
3. **Bare skills** — nessun manifest: fallback storico, tutte le skill trovate
   finiscono in `user-pack`.

## MCP server dei pack: esposti, mai auto-montati

Gli MCP server dichiarati da un pack sono **esposti dal catalogo** (config con
secret mascherati) ma **non vengono mai registrati automaticamente sul
gateway**: montarli resta un'azione esplicita dell'owner dalla sezione Tools
della webui (Prima Legge: uno zip importato non deve attivare endpoint o
processi arbitrari).

## API

- `GET /clodia/packs` — lista pack con skills/rules/mcp_servers e counts
- `GET /clodia/packs/{name}` — dettaglio singolo pack
- `POST /clodia/packs/import` — import da .zip (multipart `file`)
- `POST /clodia/packs/import-url` — import da URL (`{"url": ...}`)
- `DELETE /clodia/packs/{name}` — rimozione pack non nativo (skills + rules +
  manifest; per i pack external il marker `.external-packs/` resta, quindi la
  rimozione è durevole ai riavvii)

Le API storiche `/clodia/skills` e `/clodia/rules` restano invariate (item
singoli con `pack`/`variants`).

## Grant per-agent

In `agent.yaml`, skills e rules supportano la stessa grammatica pack-aware:

```yaml
capabilities: ["base-pack/*", "anthropic-pack/pdf"]
rules: ["secrets-handling", "acme-pack/*"]
```

`<pack>/<nome>` = elemento qualificato; `<pack>/*` = tutto il pack; `*` =
tutto il catalogo (super-agent).
