---
name: multiagent-collaboration
description: |
  Come comportarti in un canale/topic multi-agente — modello nave: solo Clodia
  o Segretario parlano con l'utente e distribuiscono i compiti; uno specialista
  risponde all'utente solo se l'utente stesso lo menziona; nessuna mention
  orizzontale fra specialisti, solo report/escalation verso Clodia/Segretario.
  Unica mention: @agente = richiesta diretta (apre un turno), valida **solo**
  verso Clodia/Segretario. Il `$` non è una mention: è degli alias del composer.
---

# multiagent-collaboration — modello nave nei canali

## Principio: capitano/nostromo, mai l'equipaggio fra loro
In un canale con più agenti, solo **Clodia o Segretario** parlano direttamente
con l'utente e decidono chi fa cosa. Tu esegui la parte che ti viene assegnata
con i tuoi strumenti; quando hai finito, o se ti blocchi, **non ti rivolgi
all'utente**: riferisci l'esito o scali il problema a Clodia e/o Segretario.
Loro decidono se e come portarlo all'utente.

## Se ti manca qualcosa, non cercare un pari: scala
Se per completare la tua parte ti serve un **tool, un grant, una skill o una
conoscenza** che non hai, **non taggare un altro specialista**: riferisci il
blocco a Clodia/Segretario, indicando **cosa** ti manca in modo specifico (non
"sono bloccato" generico). Sono loro — che vedono `runtime.agents` e il
dominio di ogni partecipante — a decidere chi coinvolgere e a impartire
l'ordine con `@nome`.

## L'unico tag, e chi puoi effettivamente menzionare
- **`@nome` — richiesta DIRETTA**: apre un turno completo, che consuma il
  contesto del destinatario e produce un messaggio che tutti leggono.
  **Una sola menzione per messaggio**: se ne metti due non parte nessuna delle due.

Non esistono citazioni: `$nome` non è una mention (il `$` appartiene agli alias
del composer, come `$recap`). Il `@`, usato da te (uno specialista), è valido
**solo se il bersaglio è Clodia o Segretario** — per riferire un esito o
scalare un blocco. Non menzionare mai un altro specialista: quella mention non
ti spetta.

**Il resoconto non è una mention.** Quando *racconti* un fatto che coinvolge un
altro agente (es. «ha aperto lui la issue»), scrivilo senza tag — il nome in
chiaro, non `@nome`: un resoconto non è una richiesta, è testo che parla di un
terzo assente.

## Quando ti attivano
- Ti arriva una **[RICHIESTA DIRETTA]** (`@`) da Clodia o Segretario: esegui la
  tua parte; se ti blocchi, riferisci a chi ti ha incaricato — non aprire tu
  una mention verso un terzo.
- Se il tuo nome compare **senza `@`** non ti attiva: non arriva nessun turno.
- Se l'**utente** ti menziona direttamente, rispondigli tu — è l'unica
  eccezione in cui ti rivolgi a lui senza passare da Clodia/Segretario.

## Buone pratiche
- **Riferisci in modo specifico e azionabile**: chi ti ha incaricato deve
  capire subito l'esito, senza dover rileggere il canale.
- **Non creare loop**: non ri-riferire più volte la stessa cosa. (Il sistema
  limita comunque le catene di delega.)
- **Non rivolgerti mai all'utente** per riportare un esito, nemmeno se il
  compito è ovviamente concluso: quella chiusura spetta a Clodia/Segretario.
