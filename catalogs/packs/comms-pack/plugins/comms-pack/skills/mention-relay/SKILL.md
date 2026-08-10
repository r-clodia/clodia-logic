---
name: mention-relay
description: |
  Protocollo del messaggero per recapitare sul gruppo Telegram collegato a un
  topic le MENZIONI che le persone di quel gruppo ricevono nel topic. Non è un
  mirror e non è un ascolto: il gateway accoda, e un JOB periodico chiama
  `telegram.notify_pending`, invia con `telegram.send` e conferma con
  `telegram.notify_ack`. Ogni fire è un turno breve.
---

# mention-relay — le menzioni arrivano a chi è nel gruppo

## Quando usarla
L'owner ha collegato un gruppo Telegram a un topic (`topic.telegram_bind`) e
vuole che le persone di quel gruppo sappiano quando vengono menzionate nella
conversazione. Non serve per mandare un messaggio una tantum: per quello c'è
`telegram.send`.

## Cosa NON è
- **Non è un mirror del topic.** Non si riporta la conversazione sul gruppo:
  escono le sole menzioni. Il modello a specchio è stato abbandonato il 18
  luglio 2026 e non va ricostruito da questo lato.
- **Non è un ascolto.** Non si resta in attesa: il gateway ha già accodato, e
  ogni fire del job è un turno breve.
- **Non componi tu il testo.** `telegram.notify_pending` restituisce il campo
  `text` già composto — chi ti ha menzionato, dove, la riga della menzione e il
  link. Riscriverlo significherebbe che il taglio dell'estratto e il link
  dipendono da come ti gira quel turno: quel giudizio sta nel gateway, dove è
  uguale per tutti e ha dei test.

## Passi, a ogni fire
1. `telegram.notify_pending(limit=20)` — le notifiche da recapitare.
   Se è vuota, **il turno finisce qui**: nessun messaggio, nessun commento.
2. Per ognuna, nell'ordine in cui arrivano:
   - `telegram.send(chat_id=<chat_id>, text=<text>)` — il testo è quello che hai
     ricevuto, verbatim;
   - se l'invio riesce: `telegram.notify_ack(message_id, chat_id, principal, ok=true)`;
   - se fallisce: `telegram.notify_ack(..., ok=false, error="<motivo breve>")` e
     **passa alla successiva**. Non ritentare dentro lo stesso turno: il
     contatore dei tentativi è nel gateway e il prossimo fire riproverà.
3. Non scrivere nulla nel topic. Una notifica che tornasse nella stanza
   creerebbe una menzione, che accoderebbe una notifica: il ciclo si chiude
   solo perché nessuno posta.

## Creare il job
Su richiesta dell'owner, `jobs.propose` con:
- `agent` = **te stesso** (il messaggero): il fire deve girare con la tua
  identità, perché è la tua che ha il grant `telegram.*`;
- intervallo **5 minuti** come default. Più stretto non serve — una menzione
  non è un allarme — e più largo fa arrivare l'avviso quando la conversazione è
  già andata avanti;
- prompt: «Esegui la skill mention-relay: recapita le notifiche pendenti».

## Se qualcosa non torna
- **Il bot non è più nel gruppo** (`403`, «bot was kicked»): non riprovare a
  entrare e non cercare un altro gruppo. Segnala all'owner nel topic — quello sì
  — che il collegamento è da rifare, e cita il `chat_id`.
- **`429` (too many requests)**: `ok=false` e basta. Il prossimo fire riprende;
  insistere nello stesso turno peggiora il rate limit.
- **Una notifica ricompare a ogni fire**: guarda `attempts` e `last_error` in
  `notify_pending`. Dopo l'ultimo tentativo smette di essere proposta e resta
  in coda leggibile — è così apposta, perché una notifica sparita in silenzio
  non direbbe a nessuno che quella persona non è stata avvisata.
