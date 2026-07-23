---
name: check-email
description: |
  Protocollo di messaggero per "restare in attesa" di una email in modo semplice e
  robusto: NON un ascolto bloccante e NIENTE ledger. Quando l'utente chiede a
  messaggero di attendere un certo messaggio, messaggero CREA UN JOB (intervallo T)
  che ad ogni esecuzione controlla una casella, filtra per subject/mittente e, se
  trova un messaggio nuovo, lo INIETTA nel topic con una @menzione a un agente (di
  norma un altro spawn di messaggero) che lo processa. Ogni fire è un turno breve;
  fra un fire e l'altro messaggero è libero.
---

# check-email — attesa di una email via job periodico

## Quando usarla
L'utente chiede a messaggero di **restare in attesa** di una email ("avvisami quando
arriva la risposta di X", "controlla se arriva una mail con oggetto Y e portala nel
topic Z"). NON per leggere la posta una tantum (per quello basta `email.search`/`read`).

## Modello (semplice, no ledger)
- L'ascolto è un **JOB periodico**, non un turno bloccante. Ogni fire = un turno
  breve; fra un fire e l'altro messaggero è idle e libero per altri ordini.
- Niente stato/ledger da mantenere: il match si fa **al volo** sulla casella
  (subject/mittente/non-letti), e l'anti-duplicato si ottiene con un filtro semplice
  (es. solo messaggi **non letti**, o più recenti di quando è partito il job).

## Passi
1. **Raccogli i parametri** dall'utente (chiedi solo ciò che manca):
   - account/casella (es. `studio`, `info@tomato.blue`) e cartella (default `INBOX`);
   - **filtro**: subject (pattern) e/o mittente;
   - **intervallo T** (default 5 min);
   - **topic di destinazione** (`tier/name`) e **@agente** da menzionare (di norma
     `@messaggero` — un altro spawn — o l'agente competente del topic).
2. **Crea il job** con `jobs.propose` (l'owner lo approva — è gated by design):
   - **`agent = messaggero`** (te stesso): il fire deve girare come messaggero,
     non come clodia — la routine usa `topic.post_message` (prerogativa messaggero)
     e le tue capability email. Passalo SEMPRE esplicito in `jobs.propose`;
   - `schedule`/`cron` = intervallo T;
   - `prompt` = la routine di check qui sotto, con i parametri risolti.
3. **Conferma** all'utente: cosa controlli, ogni quanto, dove inietti, chi menzioni.
   Il job resta attivo finché l'owner lo disabilita.

## Routine di ogni fire (nel prompt del job)
Ad ogni esecuzione, un tuo spawn:
1. `email.search`/`email.list` sulla casella+cartella, filtrando per subject/mittente
   (e **non letti**, per non ripescare i vecchi).
2. Se **nessun** match → turno silenzioso, fine.
3. Se **match** → per il messaggio nuovo:
   - leggi l'essenziale (`email.read`: mittente, oggetto, estratto);
   - **inietta nel topic** con **`topic.post_message(tier, name, text)`**, dove `text`
     inizia con la **@menzione** dell'agente scelto e riassume la mail
     (es. `@messaggero è arrivata la mail "<oggetto>" da <mittente>: <estratto>. Allegati: …`);
   - se ci sono **allegati** utili: `email.save_attachment` (byte nello scratch) →
     `topic.put` nel topic, poi citali nel messaggio.
4. Segna il messaggio come **letto** (o aggiorna il filtro) così il fire successivo
   non lo ripesca. Turno finito.

## Note
- `topic.post_message` è una tua prerogativa (messaggero) — posti una bolla nella
  chat del topic. La **@menzione** serve a far prendere in carico la mail
  dall'agente indicato (un altro spawn che, ricevuto il tag, la lavora).
- Posti solo in topic di cui sei **participant** (cross-topic → gate).
- Segreti/token mai nel contesto: i tool leggono le credenziali internamente.
- Un solo job per (casella, filtro, topic): se l'utente ne chiede un altro uguale,
  riusa/aggiorna quello esistente invece di duplicarlo.
