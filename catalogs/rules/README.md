# Rules Catalog — base-pack e pack locali

Catalogo delle **rules** di Clodia. Una rule è un file `.md` con frontmatter
`globs:` più body con knowledge contestuale. Il catalogo è runtime-neutral: gli
adapter `agent_sdk` decidono come esporre le rules al CLI/SDK agentico.

Come per le skill, il concetto utente è il **pack**:

| Pack | Significato | Dove vive oggi |
|---|---|---|
| `base-pack` | Rules native di Clodia Agency, presenti in ogni installazione. | `clodia-logic/rules-catalog/` |
| `local-pack` | Rules create, installate o modificate dall'utente in questa installazione. Include override locali di rules native. | `clodia-data/rules-catalog/` |
| `<nome>-pack` | Pack installato per un business, cliente, dominio o processo. Esempio: `acme-pack`. | di norma `clodia-data/rules-catalog/`, con metadata `pack` o naming convenzionale |

`logic` e `data` restano solo origini tecniche di storage. Le API readonly
`/clodia/rules` espongono `pack`, `source`, `available_packs` e `variants` come
per `/clodia/skills`.

## Layout fisico

```text
clodia-logic/rules-catalog/        # base-pack, in git, distribuibile
├── secrets-handling.md
├── git-commit-style.md
├── python-style.md
└── skill-authoring.md

clodia-data/rules-catalog/         # local-pack e pack installati dell'istanza
├── acme-blog-voice.md           # acme-pack
├── acme-next-conventions.md # acme-pack
└── agent-server-fastapi.md        # local-pack, se non dichiara pack
```

## Rules vs Skills vs CLAUDE.md

| | CLAUDE.md | Rules | Skills |
|---|---|---|---|
| Caricato | Sempre all'avvio | On-demand per path glob | On-demand per name/description match |
| Per cosa | Facts brevi sempre veri | Knowledge domain-specific path-triggered | Workflow attivi multi-step |
| Esempio | Stack tecnologico, convenzioni globali | "Quando tocchi .tsx leggi AGENTS.md" | "Quando devi scrivere un articolo blog Acme esegui questo workflow" |

## Override e varianti

Se una rule esiste solo nel `base-pack`, gli agenti usano quella.

Se lo stesso nome esiste anche nel catalogo data, la variante data è quella
attiva a runtime. In questo caso:

- `source = "both"`
- `available_packs = ["base-pack", "local-pack"]` o pack esplicito
- `variants` contiene sia la versione base sia quella locale

Questo consente di mantenere la rule nativa e, allo stesso tempo, avere una
versione personalizzata dell'installazione.

## Pack espliciti

Una rule data-only può dichiarare il pack nel frontmatter:

```yaml
---
pack: acme-pack
globs:
  - "**/BlogArticle*.tsx"
---
```

Chiavi supportate:

- `pack`
- `pack_id`
- `packId`

Se il pack non è dichiarato, l'API applica fallback pragmatici:

- nomi `acme-*` o `acme-*` → `acme-pack`
- altro data-only → `local-pack`
- override data di una rule base → `local-pack`

## Loader runtime

`tools/system/agent-server/server/agents/rule_sync.py` materializza ogni rule
dichiarata in `agent.yaml.rules` come file `.agent/rules/<name>.md` nel
workspace effimero.

Ordine di risoluzione:

1. `/datadir/rules-catalog/<name>.md`
2. `/clodia/rules-catalog/<name>.md`

Questa precedenza implementa l'override locale: la versione data vince, se
esiste. Gli adapter runtime convertono poi la forma neutra nel layout richiesto
dal runtime agentico.

## Dichiarare rules per un agente

```yaml
name: dev
capabilities:
  - acme-blog-integrate
rules:
  - secrets-handling
  - acme-next-conventions
  - agent-server-fastapi
```

Al boot del workspace, le rules vengono copiate in `<workspace>/.agent/rules/`.

## Convenzione file rule

```markdown
---
globs:
  - "**/*.tsx"
  - "src/components/**"
---

# Rule: <titolo>

<knowledge contestuale: cosa sapere quando lavori sui path matchati>
```

Con pack esplicito:

```markdown
---
pack: <nome-pack>
globs:
  - "**/*.tsx"
---

# Rule: <titolo>

<knowledge contestuale>
```

## Cosa va dove

| Caso | Pack consigliato |
|---|---|
| Convenzione generale utile a qualunque installazione Clodia | `base-pack` |
| Convenzione locale dell'istanza o di un repo dell'owner | `local-pack` |
| Rule che modifica una nativa mantenendo lo stesso nome | `local-pack` override |
| Set coerente di rules per business/dominio/processo installabile | `<nome>-pack`, es. `acme-pack` |

Quando una rule specifica diventa generica, può migrare da un pack locale o
business al `base-pack`.

## Rule di authoring skill

`skill-authoring` e' la rule base che mantiene separati:

- skill di dominio: input, output, workflow, verdict;
- control plane: kanban, Trello, lane, claim, commenti, move, unassign.

Quando modifichi o crei una skill, applica questa rule: solo
`kanban-operations` deve dipendere direttamente dal piano kanban/Trello.
