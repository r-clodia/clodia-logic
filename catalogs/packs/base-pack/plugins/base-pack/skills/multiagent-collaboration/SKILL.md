---
name: multiagent-collaboration
description: |
  Come lavorare in squadra in un canale/topic multi-agente: orientarsi agli
  OBIETTIVI (non ai comandi), e quando mancano risorse/tool/skill cercare nel
  canale chi può aiutare e coinvolgerlo. Convenzione dei tag: @agente = richiesta
  diretta (attiva), $agente = menzione soft (l'altro giudica se intervenire).
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
- **`@agente` — richiesta DIRETTA**: gli chiedi di fare/rispondere → lo **attiva**.
  Puoi mettere **più `@tag` nello stesso messaggio**, chiedendo cose diverse a
  ciascuno (es. `@commercialista verifica il bilancio, @avvocato controlla la
  clausola 4`). N tag → N agenti attivati (in parallelo).
- **`$agente` — menzione SOFT**: lo citi o lo informi **senza** pretendere un
  intervento. L'altro **giudica**: può rispondere se utile, o dare solo un cenno
  breve. Usalo per tenere qualcuno nel giro, dare visibilità, chiedere un parere
  facoltativo.

## Quando ti attivano
- Ti arriva una **[RICHIESTA DIRETTA]** (@): esegui la tua parte; se ti blocchi,
  applica il punto "cerca chi può aiutarti" e delega con @/$.
- Ti arriva una **[MENZIONE SOFT]** ($): intervieni **solo se hai qualcosa di utile**;
  altrimenti un cenno di una riga (es. "👍 noto, nulla da aggiungere"). Non produrre
  un intervento completo se non serve.

## Buone pratiche
- **Chiedi in modo specifico e azionabile**: l'altro deve capire subito cosa fare.
- **Non creare loop**: non ri-taggare all'infinito; se una cosa è già stata evasa,
  non riaprirla. (Il sistema limita comunque le catene di delega.)
- **Riferisci l'esito** nel canale quando finisci la tua parte, così l'obiettivo
  avanza in modo visibile a tutti.
- **Rispetta i confini**: coinvolgi solo partecipanti del canale idonei al tier; per
  portare qualcuno nuovo, proponilo all'owner (non puoi invitare tu).
