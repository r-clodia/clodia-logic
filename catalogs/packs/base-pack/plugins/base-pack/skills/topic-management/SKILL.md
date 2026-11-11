---
name: topic-management
description: |
  Protocollo OBBLIGATORIO per leggere/scrivere un topic. I topic sono uno spazio
  condiviso multi-attore, NON la memoria privata dell'agente. Si accede SOLO
  tramite i verbi `topic.*` del gateway (mai toccando lo storage direttamente):
  il gateway è il reference monitor che fa rispettare identità, classe e privacy.
  Ciclo: `topic.open` (prendi il summary_version) → lavora → `topic.save_summary`
  in optimistic-lock, e le decisioni come file datato in `files/` (`topic.put`).
  Usare ogni volta che si sta per leggere o aggiornare un topic — e SEMPRE
  quando un comando termina con `.note` o `.save` (vedi sezione Comandi).
---

# Skill: topic-management (Topic System v2)

## Modello (cosa devi sapere)
- Un **topic** è una stanza di lavoro condivisa (un bando, un cliente, un
  progetto). Vive dietro il **gateway**, su uno **storage astratto** (filesystem
  locale, Google Drive, …): a te non interessa quale — usi i verbi.
- Ogni topic ha: `meta` (title, type, status, **`tier`**, tags, people,
  `todos`, deadline, contact_agent, storage), un **`summary`** (stato corrente) e i suoi
  **file** (`files/`). `meta.todos` è la **lista to-do** strutturata (voci con
  `id`, `text`, `due`, `assignee`, `status` open|done); la **`deadline` è
  derivata**: la `due` più vicina fra le voci aperte, non si imposta a mano. Le minute non esistono più: una decisione si registra
  come file datato in `files/` (`AAAA-MM-GG-<oggetto>.md`) richiamato dal summary.
- Il **`tier`** è la sola classe del topic e insieme il suo livello di privacy:
  **P0 Public · P1 Internal · P2 Confidential · P3 Restricted**. Sostituisce
  personal/confidential.
- **Non hai accesso diretto ai file** del topic: li raggiungi esclusivamente con
  i verbi `topic.*`. È voluto (sicurezza/Prima Legge): il gateway controlla chi
  può leggere cosa.

## Verbi del gateway
- `topic.list({tier?, include_archived?})` — colpo d'occhio sui topic.
- `topic.search({query, mode})` — ritrova un topic per contenuto.
- `topic.open({tier, name})` — **read-only**: ritorna `meta`, `summary`,
  **`summary_version`**, **`todos_version`**, `tldr`. **Apri sempre prima di scrivere.**
- `topic.save_summary({tier, name, text, base_version})` — riscrive il summary.
- `topic.put` — scrive un file in `files/` (per le decisioni: file datato).
- `topic.todo_add({tier, name, text, due?, assignee?, base_version})`,
  `topic.todo_update({tier, name, id, text?, due?, assignee?, status?, base_version})`,
  `topic.todo_remove({tier, name, id, base_version})` — la lista to-do.
  `base_version` è **obbligatorio**: il `todos_version` di `topic.open` (ogni
  risposta restituisce quello nuovo). Una voce fatta si chiude con
  `status: "done"`; `todo_remove` è per una voce che non doveva esserci.
- Creare e archiviare un topic **non sono verbi degli agenti**: li fa l'owner
  dalla webui. Se serve un topic nuovo, proponilo all'owner.

## Ciclo di lavoro (sempre in quest'ordine)
1. **`topic.open`** → leggi lo stato e **conserva `summary_version`**.
2. **Lavora**: decidi cosa è cambiato.
3. Scrivi:
   - una **decisione/riunione** → file datato in `files/` con `topic.put`
     (`AAAA-MM-GG-<oggetto>.md`), richiamato dal summary.
   - **`topic.save_summary`** passando **`base_version` = il `summary_version`
     letto al punto 1** (optimistic lock).
   - action point nuovi, fatti o con una data → **lista to-do** (`topic.todo_*`
     con `base_version` = il `todos_version` letto al punto 1).
4. Se `save_summary` o un `todo_*` torna **`CONFLICT`** (qualcun altro ha scritto nel
   frattempo): **NON ritentare sovrascrivendo**. Rifai `topic.open`, **riapplica
   le tue modifiche** sul testo aggiornato e risalva. Se non riesci a riconciliare
   → **escala a owner**.

## Disciplina editoriale del `summary` (da cui dipende la card webui)
- **Prima riga = TLDR**: **UNA riga telegrafica**, massimo 120 caratteri, senza
  titolo (la card lo mostra già): stato, numeri chiave, prossima azione. Esempio:
  `Discovery chiusa. Stima 3 mesi 300k euro. Next action decidere escrow.`
  Niente frasi complete, niente premesse. Se `topic.save_summary` risponde con
  `tldr_warning`, riscrivi la riga e risalva.
- Sezione **`## Prossimi passi`**: il racconto degli action point aperti. La
  versione strutturata — con date e assegnatari, da cui deriva la deadline del
  topic — è la lista to-do (`meta.todos`): tienile allineate. Una data scritta
  solo nel summary **non** diventa la deadline del topic.
- Il summary si **riscrive** quando la sessione produce informazione nuova; non è
  un log — è lo *stato corrente*.

## Nuovo topic
Non lo crei tu: lo crea l'owner dalla webui. Se il lavoro richiede una stanza
nuova, proponila all'owner con titolo, tipo e tier suggerito (dati interni →
SEAL-1, dati cliente/riservati → SEAL-2, NDA/bilanci → SEAL-3).

## Comandi (suffissi del prompt di owner)
Convenzioni d'interazione ereditate da Clodia Primal, qui mappate sui verbi v2.

- **`.note`** (e il suo alias **`.save`**) — quando un comando termina con `.note`
  o `.save`: registra il lavoro nel topic pertinente.
  1. `topic.open` del topic (prendi `summary_version`); se non esiste, dillo all'owner.
  2. Esegui quanto richiesto nel prompt.
  3. **Aggiorna lo stato**: `topic.save_summary` (prima riga = TLDR; sezione
     `## Prossimi passi` = action point) passando `base_version`; **e** la
     decisione presa come file datato in `files/` (`topic.put`); **e** la lista
     to-do riconciliata (`topic.todo_*`).
  - Niente commit/push manuali: `save_summary`/`put` **persistono già**
    nello store via gateway. Su `CONFLICT` → rileggi e riapplica, non sovrascrivere.

(`.copy` non è qui: è un alias del *plan mode* — conferma prima di agire — non una
operazione sui topic.)

## Regole non negoziabili
- **Mai** scrivere senza aver fatto `topic.open` prima (ti serve `summary_version`,
  e `todos_version` per la lista to-do).
- **Mai** sovrascrivere su `CONFLICT`: rileggi, riapplica, o escala.
- **Il tier** non è decorativo: un topic ad alto tier (P2/P3)
  può essere inaccessibile a seconda del motore su cui giri — è il gateway a
  decidere, tu rispetti il suo verdetto (403) senza aggirarlo.
- Le **minute sono append-only**: non riscrivere o cancellare minute esistenti.
