---
name: email-reconcile
description: |
  Protocollo per messaggero: a ogni "fire" di un JOB periodico (come il backup, es.
  ogni 2-5 min) riconcilia la posta in ARRIVO nei topic, in modo deterministico e
  sicuro. Un topic riceve SOLO i reply alle conversazioni che ha INIZIATO lui: il
  match è per header di threading (In-Reply-To/References) contro un LEDGER nella
  seed memory di messaggero. Niente routing per contenuto, niente triage, nessun
  rischio di mis-route. NON è un ascolto bloccante: è una routine breve per turno,
  così fra un fire e l'altro messaggero resta libero per ordini e altre azioni.
---

# email-reconcile — inbound email → topic, deterministico, job-driven

## Modello (perché è sicuro)
Un topic possiede le conversazioni che ha **iniziato lui** e riceve **solo i reply**
a quei thread. Nessuna classificazione per contenuto, nessun topic di triage: una
mail che non risponde a un thread di un topic **non entra in alcun topic**.

- **Ascolto = job, non blocco.** Non restare in `poll` bloccante: l'ascolto è un
  **job schedulato** (agent=messaggero) che a ogni fire esegue questa routine e poi
  finisce. Fra un fire e l'altro messaggero è idle → libero per ordini/azioni.
- Concorrenza: il **ledger** nella seed memory è **stato condiviso**. Trattalo con
  disciplina **single-writer**: leggi → modifica → riscrivi l'INTERO file
  (`memory.write`/`put`) in un colpo; non tenere stato parziale fra i turni.

## Il LEDGER (seed memory)
File `email-ledger.json` nella tua seed memory (`memory.read`/`memory.write`; per
tabelle grandi `memory.put`/`memory.fetch` via scratch):
```json
{
  "watermark": { "<account>/<folder>": "<ultimo_uid_processato>" },
  "threads": {
    "<message_id_radice>": {
      "tier": "SEAL-1", "topic": "hedge-iot-new", "account": "studio",
      "ids": ["<message_id_radice>", "<message_id_reply1>", "..."]
    }
  }
}
```
`ids` = tutti i Message-ID del thread (uscita + entrata), così i turni successivi
continuano a matchare via `References`.

## Routine a ogni fire del job
1. **Leggi il ledger** dalla seed memory (se assente, inizializzalo vuoto).
2. **Nuove mail**: `email.list` (per ogni account/folder gestito) filtrando dal
   `watermark` in poi. Se il tool non filtra per uid, scarta quelle con uid ≤ watermark.
3. **Per ogni nuova mail** (`email.read` → `message_id`, `in_reply_to`, `references`):
   - cerca un match: un qualunque id in `in_reply_to`+`references` presente in un
     `threads[*].ids`.
   - **match** → **posta la mail nel topic** di quel thread (vedi sotto); poi
     **aggiungi** il `message_id` della mail a `ids` di quel thread.
   - **nessun match** → **NON instradare**: la mail resta in inbox (non è affare di
     nessun topic). Non inventare un topic, non usare oggetto/contenuto per indovinare.
4. **Avanza il watermark** all'uid più alto processato.
5. **Riscrivi il ledger** (single-writer, file intero) e termina il turno.

## Postare nel topic
- Puoi postare SOLO nei topic di cui sei **participant** (compartimento). Usa i verbi
  `topic.*`: scrivi un messaggio nel canale con mittente/oggetto/data + corpo.
- **Allegati**: `email.save_attachment` (byte nello scratch, niente base64) →
  `topic.put(src=<scratch>)`. Mai base64 grandi nei parametri.
- **Dedup**: non ripostare un `message_id` già presente in `ids` (idempotenza anche
  se il job rigira sulla stessa mail).

## Outbound — come nasce un thread di un topic
Quando un topic ti chiede di **iniziare** una conversazione email:
1. invia con `email.send` (o `email.reply` se stai rispondendo a una mail già a ledger);
2. **registra il thread**: `threads[<message_id_uscente>] = {tier, topic, account, ids:[...]}`.
   - `email.reply` mantiene già il threading; il thread è quello della mail matchata.
   - `email.send` (nuovo thread) **oggi non restituisce il message_id**: recuperalo
     leggendo la cartella **Sent** subito dopo l'invio (`email.list --folder Sent`,
     prendi il più recente per oggetto/destinatario) e usalo come chiave del ledger.
     (Quando `send` restituirà il message_id, salta questo passaggio.)

## Regole non negoziabili (Prima Legge)
- **Solo reply a thread del topic**: un reply entra SOLO nel topic che ha aperto il
  thread → tier e partecipanti **ereditati**, mai scelti. Nessuna down-classification.
- **Cold-mail**: mai auto-instradata in un topic. Resta in inbox.
- **Segreti**: token/credenziali mai nel contesto; le legge il tool internamente.
- **Idempotenza**: watermark + `ids` evitano doppi post e riprocessi.

## Report
Il turno del job è silenzioso se non c'è nulla da fare. Se instradi mail, un log
sintetico: quante mail, in quali topic. In caso di errore su una mail, salta quella
(non bloccare le altre) e segnalalo nel log.
