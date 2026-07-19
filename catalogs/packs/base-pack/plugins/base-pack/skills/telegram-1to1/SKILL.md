---
name: telegram-1to1
description: |
  Protocollo per una conversazione SINCRONA 1:1 con l'umano (Davide) via Telegram:
  invii un messaggio e RESTI IN ATTESA della sua risposta, in loop, finché lui non
  chiude. Da usare quando durante un task serve input/decisioni umane in tempo reale
  e l'umano non è sulla webui (es. è fuori, segue dal telefono). Specifico Telegram,
  con due declinazioni: Mac/Clodia Primal (curl + bot daemon) e agent dell'istanza
  (verbi telegram.* del gateway). NON usare per semplici notifiche one-shot (per
  quelle basta un singolo invio, senza acquisire il canale).
---

# telegram-1to1 — conversazione sincrona 1:1 con l'umano via Telegram

## Quando usarla
- Un task lungo richiede **decisioni/approvazioni umane in tempo reale** e l'umano
  segue da Telegram (non dalla webui).
- L'umano lo chiede esplicitamente ("apriamo un Telegram 1:1", "scrivimi su Telegram
  e aspetta le mie risposte").

**Quando NO**: per una notifica secca (milestone, PR aperta) → un solo `sendMessage`,
senza acquisire/rilasciare il canale.

## Il protocollo (5 fasi)
1. **Acquisisci il canale** — garantisci che ci sia **un solo consumer** di
   `getUpdates` per quel bot, altrimenti i messaggi si perdono.
2. **Notifica** — invia il messaggio all'umano.
3. **Attendi la risposta** — ascolto in **long-poll**, filtrando per **identità
   autorizzata** e avanzando l'`offset` per non riprocessare.
4. **Loop** — leggi → se è un'istruzione operativa rispondi **"ricevuto"** e
   procedi → torna ad attendere. L'attesa è **indefinita** (vedi chiusura).
5. **Rilascia il canale** — SEMPRE, anche su errore/timeout (blocco `finally`).

## Regole non negoziabili
- **Token mai nel contesto**: leggilo dai secret via espansione shell
  (`$(cat secrets/telegram_bot_token)` / vault), non stamparlo né incollarlo.
- **Solo l'umano autorizzato**: processa SOLO i messaggi dal `chat_id`/principal di
  Davide (Mac: `76632169`); ignora gli altri mittenti.
- **Offset**: dopo ogni update letto, riparti da `offset = update_id + 1`.
- **"ricevuto"**: dopo ogni messaggio di Davide che dà istruzioni, conferma con un
  "ricevuto" e poi esegui — così sa che il messaggio è arrivato.
- **Rilascio garantito**: a fine conversazione il canale torna come prima (bot
  riacceso / lease rilasciato), sempre.

## Protocollo radio (turni)
Stile radiocomunicazione, per turni netti:
- **Ack immediato**: appena arriva un messaggio dell'umano, rispondi subito
  **"ricevuto"** (prima di eseguire), così sa che è arrivato. Poi esegui.
- **Parola di fine**: la frase canonica con cui l'umano chiude è **"passo e chiudo"**.

## Chiusura (parola di fine + timeout)
La conversazione si chiude quando si verifica UNA delle due:
- **"passo e chiudo"** dall'umano → fine conversazione, rilascia il canale.
- **Timeout di silenzio**: nessuna risposta per **45 minuti** (default) → invia un
  ultimo messaggio ("chiudo il canale per inattività, riattivami quando vuoi") e
  rilascia. Evita di lasciare il bot spento/leasato all'infinito.

> **Bot distinti per contesto**: Clodia **Primal** (Mac) e Clodia **Personal**
> (istanza) usano **bot Telegram diversi** (token distinti). Le due declinazioni
> qui sotto non condividono mai lo stesso token → nessun conflitto di `getUpdates`
> tra i due contesti.

## Declinazione A — Mac / Clodia Primal (curl + bot daemon)
Sul Mac il consumer di `getUpdates` è il **bot daemon** (`daemons/telegram/bot.js`):
va **spento** per la durata del 1:1, e **riacceso** alla fine.
```bash
TOKEN=$(tr -d '\n\r' < secrets/telegram_bot_token); CHAT=76632169
# 1) acquisisci: spegni il bot daemon
bash daemons/telegram/stop.sh
# 2) notifica
curl -s "https://api.telegram.org/bot$TOKEN/sendMessage" \
  --data-urlencode chat_id=$CHAT --data-urlencode text="…" -o /dev/null
# 3) attendi (long-poll); filtra chat_id==Davide; avanza offset
curl -s "https://api.telegram.org/bot$TOKEN/getUpdates?offset=$OFF&timeout=50" --max-time 60
# 4) loop su 2-3 finché parola-di-fine o timeout 45'
# 5) rilascia: riaccendi il bot (SEMPRE)
bash daemons/telegram/start.sh
```

## Declinazione B — agent dell'istanza (verbi telegram.* del gateway)
Sull'istanza il consumer di `getUpdates` è il **gateway** (namespace `telegram.*`,
lease per-chat): NON si spegne alcun bot, si **acquisisce il lease** sulla chat.
- **acquire** → `telegram.lease_acquire(chat_id, minutes)` (rinnova prima della
  scadenza durante una conversazione lunga)
- **notify** → `telegram.send(chat_id, text)`
- **await** → `telegram.poll(chat_id)` in loop (consuma i messaggi della chat)
- **release** → `telegram.lease_release(chat_id)`
- scopri la chat con cui parli via `telegram.inbox` (solo chat che hanno scritto).

## Report
Alla chiusura, riassumi cosa è stato deciso/fatto durante il 1:1 e conferma che il
canale è stato rilasciato (bot riacceso / lease rilasciato).
