---
name: multiagent-collaboration
description: |
  Come lavorare in squadra in un canale/topic multi-agente: orientarsi agli
  OBIETTIVI (non ai comandi), e quando mancano risorse/tool/skill cercare nel
  canale chi può aiutare e coinvolgerlo. Convenzione dei tag: @agente = richiesta
  diretta, una per messaggio (apre un turno); $agente = citazione (non apre nulla).
---

# multiagent-collaboration — gioco di squadra nei canali

## Principio: goal-oriented, non command-oriented
Quando sei assegnato a qualcosa in un canale, ragiona sul **fine**, non sulla lettera
del comando. Il tuo compito è portare a casa l'obiettivo, anche se questo richiede di
**coinvolgere altri**. Non sei un esecutore isolato: sei un membro di una squadra.

## Se ti manca qualcosa, cerca chi può aiutarti
Se per completare la tua parte ti serve un **tool, un grant, una skill o una
conoscenza** che non hai:
1. **Guarda chi c'è nel canale** e cosa sa fare: `runtime.agents` elenca gli agenti
   con dominio (expertise), skill, knowledge (RAG) e grant. Confronta ciò che ti
   manca con ciò che gli altri partecipanti hanno.
2. **Coinvolgi lo specialista giusto** con un tag (vedi sotto), chiedendogli in modo
   **specifico** la parte che ti serve (non "aiutami" generico: dì *cosa* ti serve).
3. **Preferisci coinvolgere** l'agente competente piuttosto che fare male una cosa
   fuori dal tuo dominio. Meglio una squadra che un tuttofare.

## I due tag (convenzione del canale)
- **`@agente` — richiesta DIRETTA**: gli chiedi di fare/rispondere → lo **attiva**,
  cioè gli apre un turno completo che consuma il suo contesto e produce un messaggio
  che tutti leggono. **UNA sola menzione per messaggio**: se ne metti due non parte
  nessuno dei due, ti viene chiesto quale intendevi e quel turno lo paghi. Se ti
  servono in due, chiama il primo adesso e il secondo quando ha finito — avrai anche
  il suo esito da passargli.
- **`$agente` — CITAZIONE**: lo nomini o lo informi. **Non gli apre nessun turno** e
  non gli chiede nulla: legge il canale al suo prossimo intervento. Usalo per tenere
  qualcuno nel giro, dare visibilità, ringraziare. Se ti serve una sua azione
  **adesso**, l'unica strada è `@`.

**Il caso in cui si sbaglia il sigillo: il resoconto.** Quando *racconti* un fatto
che coinvolge un altro agente, scrivi `$`, non `@` — «`$sysadmin` ha aperto la
issue», «`$fullstack-dev` è stato taggato ieri», «il piano approvato da `$clodia`».
Un `@` in una frase di racconto convoca **davvero**: apre un turno a chi non ti aveva
chiesto niente, e se nello stesso messaggio c'è anche una richiesta vera diventano
due menzioni, quindi zero turni e una domanda. In dubbio, `$`: chi serve davvero lo
si chiama al passaggio dopo, mentre un `@` di troppo non si ritira.

## Quando ti attivano
- Ti arriva una **[RICHIESTA DIRETTA]** (@): esegui la tua parte; se ti blocchi,
  applica il punto "cerca chi può aiutarti" e delega con @/$.
- Una **citazione** (`$`) non ti attiva: non arriva nessun turno da istruire. La
  leggi nella storia del canale al tuo prossimo intervento, quando puoi già reagire
  sapendo com'è finita.

## Buone pratiche
- **Chiedi in modo specifico e azionabile**: l'altro deve capire subito cosa fare.
- **Non creare loop**: non ri-taggare all'infinito; se una cosa è già stata evasa,
  non riaprirla. (Il sistema limita comunque le catene di delega.)
- **Riferisci l'esito** nel canale quando finisci la tua parte, così l'obiettivo
  avanza in modo visibile a tutti.
- **Rispetta i confini**: coinvolgi solo partecipanti del canale idonei al tier; per
  portare qualcuno nuovo, proponilo all'owner (non puoi invitare tu).
