# Packs and plugins — the shape of the catalogue

Two levels (decided 4 Jul 2026):

```
pack   := [agent seeds] + [plugins]        # Clodia's unit of distribution
plugin := [skills] + [rules] + [mcp]       # the Claude Code standard
```

**No component is ever mandatory.** A plugin may be a single skill; a pack may
carry only a seed, only plugins, or any combination. Plugins may also live
**loose**, outside any pack. The web UI (Packs page) walks the catalogue as a
tree: pack → (agents | plugins) → skills / rules / MCP servers.

## Plugin

| component | path |
|---|---|
| skills | `CLODIA_DATA/skills-catalog/<plugin>/<skill>/SKILL.md` |
| rules | `CLODIA_DATA/rules-catalog/<plugin>/<rule>.md` |
| manifest (metadata + `mcp_servers`) | `CLODIA_DATA/plugins/<plugin>/plugin.yaml` |

Two plugins are implicit and can be neither imported nor removed:
**`base-pack`** (the catalogue bundled in this repository) and **`local-pack`**
(flat entries in the data catalogue). Historical names — `anthropic-pack`,
`user-pack` — stay as they are: they are labels, the entity is the plugin.

Origins (`origin`): `logic`, `local`, `external` (from `external-packs.yaml` at
setup), `user`, `imported`. Only external, user and imported may be deleted.

Formats recognised by `POST /clodia/plugins/import[-url]`:

1. **Claude plugin** — `.claude-plugin/plugin.json` (plus `skills/`, `.mcp.json`)
2. **Clodia plugin** — `plugin.yaml` (legacy `pack.yaml`, v6.57) plus skills/rules/mcp
3. **Bare skills** — no manifest → `user-pack`

## Pack

The shape of a pack (the `clodia-packs` repository is a directory of them):

```
<pack>/
├── pack.yaml               # name, description, version
├── agents/<seed>/          # agent.yaml + system-prompt.md + memory/ (+ pfp.png)
└── plugins/<plugin>/       # each one a plugin (plugin.json/plugin.yaml, or bare)
```

Runtime manifest: `CLODIA_DATA/packs/<pack>/pack.yaml` (name, description,
version, source, agents, plugins).

**Import** (`POST /clodia/packs/import[-url]`, unified): if the archive is a
pack, plugins and seeds are installed; otherwise it delegates to the plugin
import (`kind: "pack" | "plugin"` in the response).

**Directory of packs**: a repository holding `packs/<n>/pack.yaml` — such as
`clodia-packs` — imports every `packs/<n>/` as a pack of its own, so one import
of the repository URL installs them all with their seeds and plugins
(`kind: "packs"`). This takes precedence over marketplace detection.

**Claude marketplace**: a repository with `.claude-plugin/marketplace.json` —
the standard Claude Code uses to distribute several plugins — is recognised as a
pack. Name and description come from the marketplace, plugins from the `source`
entries declared in `plugins[]` (a missing source, or one outside the
repository, is an explicit error), seeds from `agents/` or `seeds/` directories
if present (a Clodia extension). Plugins present in the repository but **not
declared** are not imported.

**Installing a seed** installs *and registers* it: a copy into
`CLODIA_DATA/agents/<name>/`, a PKI certificate issued — without one the agent
cannot authenticate to the gateway and sees **zero** tools — `registry.load()`,
and the gateway's view refreshed. PKI and registration are best-effort, since
the entrypoint runs `issue-all` at every boot. An existing seed is **not**
overwritten (`status: exists`), and the native names are refused.

**`requires_plugins`** in a seed's `agent.yaml` is a **soft** prerequisite:

```yaml
requires_plugins:
  - name: eu-project-design
    hard: false        # the default; the short form "- eu-project-design" also works
```

A missing plugin does not stop the agent: it starts degraded, and the packs API
exposes `missing_plugins` so the UI can warn. `hard: true` is declarative today —
no boot-time enforcement — and reserved for a future policy.

**Delete** (`DELETE /clodia/packs/{name}`): removes the pack's plugins, its
non-native agents, and the manifest.

## A plugin's MCP servers auto-mount only from trusted sources

Mounting an MCP server means starting the process or URL its manifest declares
(`command` / `args`) — that is **code execution**. The rule follows from the
First Law: an imported zip must not start arbitrary processes.

- **Imported from outside** (a pack or plugin from a zip or URL): the mount is
  **not** automatic. Declared servers are reported in `mcp_mount.pending`, and
  the owner mounts them explicitly from the Tools page after review. The human
  barrier between *import* and *execution* stays.
- **Updating a first-party pack** from its own upstream (our code): the mount is
  automatic (`trusted`). If the gateway refuses or a server fails to start, the
  outcome is not hidden: `mcp_mount.failed` names the plugin, the server and the
  detail.

Seeds, by contrast, are registered at import: they are agents of the platform,
not external endpoints, and they stay inert until someone speaks to them or
schedules them. Catalogue configuration is exposed with secrets masked.

## API

- `GET /clodia/packs` · `GET /clodia/packs/{name}` — packs with their agents
  (installed, `requires`/`missing_plugins`) and resolved plugins
- `POST /clodia/packs/import` · `/import-url` — unified pack|plugin import
- `DELETE /clodia/packs/{name}`
- `GET /clodia/plugins` · `GET /clodia/plugins/{name}` — every plugin, loose ones included
- `POST /clodia/plugins/import` · `/import-url` · `DELETE /clodia/plugins/{name}`
- `/clodia/skills` and `/clodia/rules` are unchanged (items carry `pack` / `variants`)

## Per-agent grants

In `agent.yaml`, skills and rules use the plugin-aware grammar:

```yaml
capabilities:
  - base-pack/topic-management
  - base-pack/topic-files
  - base-pack/topic-drive-sync
  - base-pack/multiagent-collaboration
  - base-pack/team-composition
  - eu-project-design/*
rules: ["secrets-handling", "acme-pack/*"]
```

`<plugin>/<name>` is a qualified element; `<plugin>/*` is the whole plugin; `*`
is the whole catalogue.
