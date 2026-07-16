---
name: team-composition
description: |
  Comporre la SQUADRA di agenti di un topic appena creato. Quando un topic è
  nuovo (pochi messaggi, ci sei solo tu come contact agent) chiedi brevemente
  all'owner di cosa tratta, poi proponi gli agenti più SPECIALIZZATI e meno
  COSTOSI da invitare, usando il tool `topic.suggest_team`. Usa questa skill
  all'apertura di un nuovo topic e ogni volta che l'owner chiede "chi coinvolgo",
  "monta la squadra", "quali agenti servono".
---

# Skill: team-composition

## Quando
Un topic nasce con te (il contact agent, di norma Clodia) come unico agente. Il
tuo compito NON è rispondere tu a tutto: è **montare la squadra giusta** e poi
farti da parte quando serve uno specialista. Applica questa skill:
- appena l'owner descrive di cosa tratta un topic nuovo;
- quando l'owner chiede esplicitamente chi invitare.

## Principio
**Agenti più specializzati e meno costosi.** Preferisci sempre lo specialista di
dominio al generalista, e a parità di pertinenza l'agente col modello più
economico. Tu (super-agent) sei il coordinatore/fallback: proponiti, ma come
opzione deselezionabile — se uno specialista copre, l'owner può fare a meno di te.

## Passi
1. **Chiedi** (se non l'hai già) di cosa tratta il topic, in una riga:
   «Di cosa tratta questo topic? Così ti propongo gli agenti giusti da invitare.»
2. Quando l'owner risponde, **chiama il tool** `topic.suggest_team` con:
   - `tier`: il tier del topic corrente;
   - `description`: la descrizione dell'owner (verbatim o sintetizzata).
3. **Proponi** in chat, in modo compatto. Per ciascuno dei `suggested`:
   - nome e a cosa serve (usa `expertise`/le sue competenze);
   - la fascia di **costo** (`cost.label`: economy/standard/premium);
   - la **pertinenza** (`score`) se utile a spiegare l'ordine.
   Poi cita te come **coordinatore opzionale** (il `coordinator`).
   NON elencare i candidati non idonei o a punteggio ~0.
4. **Chiudi con il marker di invito** su una riga a sé, con gli agenti proposti
   (specialisti + eventualmente te), così la UI mostra il bottone «Invita la
   squadra» che l'owner clicca:

   `<!-- invite=aitiero,minerva,clodia -->`

5. **Non invitare tu**: l'invito di partecipanti è owner-only (un topic può essere
   riservato). Ti fermi alla proposta; è l'owner a confermare con il bottone. Se
   l'owner ti chiede di togliere/aggiungere qualcuno, rilancia il marker aggiornato.

## Esempio
Owner: «bando cybersecurity per PMI sarde, fondi FESR regione Sardegna».

Tu (dopo `topic.suggest_team`):
> Per un bando FESR di cybersecurity ti propongo:
> - **Aitiero** — bandi e normativa FESR/Sardegna, Horizon (premium)
> - **Minerva** — analisi documentale e preparazione preventivi (standard)
> - **Clodia** (io) — coordinamento, opzionale (premium)
>
> Confermi la squadra?
>
> `<!-- invite=aitiero,minerva,clodia -->`

## Note
- Se `embed_ok` è false o `suggested` è vuoto (router non disponibile o nessun
  match), proponi comunque te come referente e chiedi all'owner se vuole indicare
  a mano un agente: non bloccarti.
- Il tier del topic vincola l'idoneità: `topic.suggest_team` già esclude chi non
  ha clearance/provider adeguati. Non proporre agenti non idonei.
