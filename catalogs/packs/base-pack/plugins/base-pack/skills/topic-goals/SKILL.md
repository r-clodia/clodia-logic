---
name: topic-goals
description: |
  Come si porta a casa un OBIETTIVO di canale: il messaggio che l'owner ha
  fissato col 🎯 e che vive nel meta del topic (`meta.goal`, lo leggi con
  `topic.open`). Un obiettivo NON è una richiesta qualsiasi: è un requisito
  vincolante: resta finché l'owner non lo toglie o non ne accetta l'esito, e se
  un passo fallisce va ripreso, non abbandonato. La skill descrive le tre fasi —
  scrivere la strategia e farla approvare, eseguirla coordinando gli altri
  agenti, dichiararla raggiunta per la verifica dell'owner — il formato del
  documento di strategia e l'uso del verbo `topic.goal_progress`.
  Usare quando un canale ha un obiettivo fissato: alla sua comparsa, a ogni
  ripresa del lavoro, e prima di dichiararlo raggiunto.
---

# topic-goals — un obiettivo si porta a termine, non si prova

## Dove vive, e perché lì
L'obiettivo sta nel **meta del canale** (`meta.goal` in `topic.open`), non fra i
messaggi: una richiesta sepolta sotto duecento righe smette di esistere, un
campo del meta no. Lo leggi all'inizio di ogni turno in cui il canale ne ha uno.

```yaml
goal:
  text: "Portare il sito in produzione entro ottobre"
  message_id: "20260930-180000-abcd"   # il messaggio ORIGINALE: leggilo, `text` è troncato
  state: pinned
  pinned_by: davide
  strategy_path: local/goals/sito-in-produzione.md
```

Il ciclo di vita, e **di chi è la mossa** in ognuno:

| `state` | Chi deve muoversi | Cosa si aspetta |
|---|---|---|
| `pinned` | **tu** | scrivere la strategia e sottoporla |
| `strategy-review` | l'owner | approva (→ `in-progress`) o fa correggere |
| `in-progress` | **tu** | eseguire, coordinando gli altri agenti |
| `claimed-done` | l'owner | accetta (→ `done`) o rimanda indietro |
| `done` | nessuno | chiuso |

Tu avanzi con **`topic.goal_progress(tier, name, state, strategy_path)`**. Non
puoi fissare né togliere un obiettivo, e non puoi metterlo a `done`: quelli sono
atti dell'owner. Se provi, il verbo rifiuta — non è un guasto, è il confine.

## Fase 1 — la strategia (stato `pinned`)

**Non eseguire niente in questa fase.** Qui si decide il piano; attuarlo prima
del sì dell'owner significa aver fatto lavoro che lui non ha approvato, ed è la
cosa che il passaggio di approvazione esiste per impedire.

1. Leggi il **messaggio originale** (`message_id`), non solo `goal.text`: è una
   copia troncata a 4000 caratteri.
2. Scomponi in **passi verificabili**. Un passo è buono se si può dire senza
   discutere se è fatto o no. «Migliorare le performance» non lo è, «TTFB sotto
   400 ms sulla home misurato da X» sì.
3. Segna le **dipendenze** vere. Due passi senza dipendenza fra loro vanno
   eseguiti **insieme**, da agenti diversi: è il motivo per cui la strategia
   dichiara le dipendenze invece di essere un elenco in fila.
4. Scrivi il documento nei file del canale — `local/goals/<slug>.md` — con
   `topic.write_file`, e poi:

```
topic.goal_progress(state="strategy-review", strategy_path="local/goals/<slug>.md")
```

5. **Dillo anche in chat**, in tre righe: cosa hai capito, in quanti passi, cosa
   serve dall'owner. Il documento è la fonte, il messaggio è ciò che si legge.

### Formato del documento

```markdown
# Obiettivo: <testo dell'obiettivo>
> fissato da <chi> · messaggio `<message_id>`

## Fatto significa
- <criterio verificabile 1>
- <criterio verificabile 2>

## Cosa serve dall'owner
- <decisioni, accessi, credenziali — oppure "niente">

## Passi
| # | Passo | Chi | Dipende da | Stato |
|---|-------|-----|-----------|-------|
| 1 | Censire le pagine lente | fullstack-dev | — | da fare |
| 2 | Comprare il dominio | owner | — | da fare |
| 3 | Deploy in produzione | sysadmin | 1, 2 | da fare |

## Registro
- <data> — <cosa è successo, cosa ha cambiato il piano>
```

Stati di un passo: `da fare`, `in corso`, `fatto`, `bloccato: <perché>`.
Il **Registro** non è decorazione: è l'unica cosa che permette a un altro
spawn — o a te dopo un riavvio — di riprendere senza rifare.

## Fase 2 — l'esecuzione (stato `in-progress`)

Sei l'orchestratore, non l'esecutore. Per ogni passo pronto (dipendenze
`fatto`) convoca l'agente competente con **una menzione per messaggio**; i passi
indipendenti si mandano avanti **insieme**, a persone diverse.

Dopo ogni passo **aggiorna il documento** (stato del passo + riga nel
Registro). Un piano che non registra l'avanzamento costringe chi riprende a
ricostruirlo dalla chat, che è il lavoro che la strategia doveva evitare.

**Se un passo fallisce**: l'obiettivo è un requisito, non un tentativo. Marca
`bloccato: <perché>`, cerca la causa — per i guasti di piattaforma chiedi a
sysadmin — e riprendi. Non restare in silenzio: se sei bloccato davvero e serve
una decisione umana, scrivilo in chat dicendo **cosa** ti serve, in una riga.

## Fase 3 — dichiararlo raggiunto (`claimed-done`)

Prima di dichiarare, rileggi **«Fatto significa»** e verifica i criteri uno per
uno. `claimed-done` è una richiesta di verifica, non un verdetto: l'owner
accetta (`done`) o rimanda indietro il lavoro.

```
topic.goal_progress(state="claimed-done")
```

e nello stesso turno scrivi in chat **cosa** è stato fatto rispetto ai criteri,
non quanto hai lavorato. Se un criterio non è stato raggiunto ma consideri
l'obiettivo comunque soddisfatto, dillo esplicitamente lì: un criterio saltato
in silenzio è il motivo per cui un esito viene rifiutato due volte.

Se l'owner rimanda indietro, riparti da **quello che manca** — non dall'intero
piano.

## Il pin che sparisce
L'owner può togliere il pin in qualunque momento: `meta.goal` non c'è più.
Quando accade, **fermati**: il lavoro sull'obiettivo si interrompe lì. Non è un
errore e non va discusso — è l'interruttore che l'owner ha per fermarti.
