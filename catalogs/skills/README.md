# Skills Catalog — base-pack e pack locali

Catalogo delle **skill operative agency** di Clodia. Una skill è una cartella
`<nome>/` contenente `SKILL.md` (frontmatter `name`/`description`/altri
metadati) più eventuali asset.

La distinzione importante per utenti e agenti non è "logic vs data", ma **da
quale pack arriva la skill**.

## Pack model

| Pack | Significato | Dove vive oggi |
|---|---|---|
| `base-pack` | Skill native di Clodia Agency. Ogni installazione le riceve come dotazione iniziale. | `clodia-logic/skills-catalog/` |
| `local-pack` | Skill create, installate o modificate dall'utente in questa installazione. Include gli override locali di skill native. | `clodia-data/skills-catalog/` |
| `<nome>-pack` | Pack installato per uno specifico business, cliente, dominio o processo. Esempio: `acme-pack`. | di norma `clodia-data/skills-catalog/`, con metadata `pack` o naming convenzionale |

`logic` e `data` restano termini tecnici di storage:

- `logic` = bundle distribuibile, contenuto nel repo `clodia-logic`
- `data` = dati dell'istanza, contenuti in `CLODIA_DATA`

Le API readonly `/clodia/skills` espongono entrambi:

- `pack`: label utente effettiva, es. `base-pack`, `local-pack`, `acme-pack`
- `source`: origine tecnica, `logic`, `data` o `both`
- `available_packs`: varianti disponibili per quel nome skill
- `variants`: path e origine di ogni variante, con `active=true` sulla variante usata a runtime

## Layout fisico

```text
clodia-logic/skills-catalog/       # base-pack, in git, distribuibile
├── feature-spec/
├── code-review/
├── fact-check/
└── ...

clodia-data/skills-catalog/        # local-pack e pack installati dell'istanza
├── code-review/                   # override locale della skill base
├── acme-blog-writing/           # acme-pack
├── acme-visual/
└── ...
```

## Override e varianti

Se una skill esiste solo nel `base-pack`, gli agenti usano quella.

Se lo stesso nome esiste anche nel catalogo data, la variante data è quella
attiva a runtime. In questo caso:

- `source = "both"`
- `available_packs = ["base-pack", "local-pack"]` o pack esplicito
- `variants` contiene sia la versione base sia quella locale

Esempio: `code-review` può esistere nel `base-pack` come skill nativa e in
`clodia-data/skills-catalog/code-review/` come versione personalizzata
dell'installazione. La UI può quindi mostrare che la skill attiva è locale e
che esiste anche la variante base.

## Pack espliciti

Una skill data-only può dichiarare il pack nel frontmatter:

```yaml
---
name: my-domain-skill
pack: finance-pack
description: |
  Workflow specifico per il dominio finance.
---
```

Chiavi supportate:

- `pack`
- `pack_id`
- `packId`

Se il pack non è dichiarato, l'API applica fallback pragmatici:

- nomi `acme-*` o `acme-*` → `acme-pack`
- altro data-only → `local-pack`
- override data di una skill base → `local-pack`

## Pack esterni (caricati al setup)

I pack esterni (es. `anthropic-pack`, `openai-curated-pack`) sono dichiarati in
`catalogs/external-packs.yaml` e installati **al setup iniziale** da
`server/setup/external_packs.py`: clona ogni repo (shallow) e copia le skill in
`/datadir/skills-catalog/<pack>/<skill>/`. Il pack è dato dal **path**, quindi i
`SKILL.md` originali NON vengono modificati e i file `LICENSE`/`NOTICES` viaggiano
intatti. Idempotente (marker per pack in `skills-catalog/.external-packs/`) e
tollerante agli errori di rete. Lo **stesso nome** può esistere in più pack (es.
`pdf`) senza collisioni: in catalogo sono varianti distinte.

## Loader runtime

Lo `skill_sync` risolve ogni capability (data precede logic):

1. qualificata `<pack>/<skill>` → `/datadir/skills-catalog/<pack>/<skill>/`
2. bare `<cap>` → `/datadir/skills-catalog/<cap>/` (flat)
3. bare `<cap>` → `/datadir/skills-catalog/<pack>/<cap>/` (primo pack che la contiene)
4. `/clodia/skills-catalog/<cap>/` (base-pack)

La versione data vince, se esiste. La skill risolta viene copiata in
`<workspace>/.agent/skills/<cap>/` (le qualificate diventano `<pack>__<skill>/`
per evitare collisioni a runtime).

**Implementazione**:
`tools/system/agent-server/server/agents/skill_sync.py`

Gli adapter runtime partono dalla forma neutra `.agent/skills/` e la espongono
nel formato richiesto dal CLI/SDK agentico scelto.

## Dichiarare skill per un agente

Una capability dichiarata in `agent.yaml`:

```yaml
name: illustrator
capabilities:
  - acme-visual
  - kanban-operations
```

Al boot del workspace effimero, `materialize_capabilities()` copia ogni skill
risolta. Capability non risolte sono loggate come warning ma non bloccano il
boot.

## Cosa va dove

| Caso | Pack consigliato |
|---|---|
| Workflow generico, utile a qualunque installazione Clodia | `base-pack` |
| Skill nata per un singolo owner o una singola installazione | `local-pack` |
| Skill che modifica una nativa mantenendo lo stesso nome | `local-pack` override |
| Set coerente di skill per un business/dominio/processo installabile | `<nome>-pack`, es. `acme-pack` |

Quando una skill specifica diventa generica, può migrare da un pack locale o
business al `base-pack`.

## Skill di dominio vs control plane

Le skill devono essere agnostiche rispetto al modo in cui vengono invocate.
Una skill puo arrivare da chat, job, API, kanban o altro orchestratore futuro:
il suo compito e' descrivere il **lavoro di dominio**, non il lifecycle del
task.

Regola pratica:

- una skill descrive input, output, workflow, verdict e vincoli;
- `kanban-operations` descrive claim, commento, move, pass-forward,
  pass-back, cannot-handle e unassign;
- nomi board, nomi lane e API Trello non vanno nelle skill di dominio.

La rule `skill-authoring` in `rules-catalog/` rende esplicito questo contratto.

## Convenzioni file

```text
skills-catalog/<nome>/
├── SKILL.md          # frontmatter + workflow, obbligatorio
├── <asset>.png       # logo, icona, esempio, opzionale
├── style.md          # styleguide ulteriore, opzionale
└── examples/         # esempi end-to-end, opzionale
```

Frontmatter minimo:

```yaml
---
name: <nome-skill>
description: |
  <descrizione operativa: cosa fa la skill, quando usarla, parametri attesi,
  cosa NON fa>
---
```

Frontmatter con pack esplicito:

```yaml
---
name: <nome-skill>
pack: <nome-pack>
description: |
  <descrizione operativa>
---
```

## Aggiungere una skill nuova

### Skill nativa Clodia → base-pack

1. Crea `skills-catalog/<nome>/` nel repo `clodia-logic`
2. Crea `SKILL.md` col frontmatter
3. Apri PR su `clodia-logic`

### Skill locale o installata → local-pack / business pack

1. Crea `/datadir/skills-catalog/<nome>/` dentro il container, oppure
   `~/clodia-data/skills-catalog/<nome>/` sul server
2. Crea `SKILL.md`
3. Aggiungi `pack: <nome-pack>` se non deve essere `local-pack`
4. Per renderla disponibile a un agente, aggiungi `<nome>` in
   `capabilities:` di `agent.yaml`
5. Ricarica gli agenti con `POST /api/agents/reload`
