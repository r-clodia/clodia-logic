---
name: article-spec
description: |
  Trasforma una idea, news o angle grezzo in una spec articolo strutturata, pronta per fact-check e scrittura. Produce angle, target reader, lead, struttura H2/H3, fonti da verificare, take-away, vincoli di lunghezza/voce e hint per visual/social.

  Skill di dominio trasferibile a qualunque brand editoriale. Brand voice e regole specifiche arrivano da rules o pack locali.
---

# Skill: article-spec

Step preliminare alla stesura. Senza spec robusta, il writer produce articoli
fuori target, con angle debole o non aderenti all'intento editoriale.

## Quando si applica

- Idea articolo, news pickup o brief editoriale ancora grezzo.
- Serve trasformare l'idea in brief eseguibile per fact-check e draft.
- Il brand ha vincoli di voce, target o topic da rispettare.

## Input

- Titolo o tema.
- Descrizione raw, link, fonti, note editoriali.
- Articoli pubblicati recentemente, se disponibili.
- Rules di brand o dominio, se disponibili.

## Output

```markdown
**Angle**
<1-2 frasi: cosa diciamo noi che altri non dicono>

**Target reader**
<persona, contesto, maturita, bisogni>

**Lead (proposta)**
<1 frase con insight concreto: data, numero, normativa o tensione reale>

**Struttura H2/H3**
- H2: <sezione 1>
  - H3: <punto specifico>
- H2: <sezione 2>
- H2: Cosa fare adesso

**Fonti da verificare**
- <fonte primaria 1>
- <fonte primaria 2>

**Take-away**
- **<bold opener>**: <azione concreta>

**Vincoli**
- Lunghezza: <range>
- Voice: <rule/pack di riferimento>
- Lingue: <se applicabile>
- Cross-link: <se rilevante>

**Note implementazione**
- Cover hint: <opzionale>
- Social hint: <opzionale>
```

## Workflow

### 1. Carico contesto

- Leggi input raw e fonti citate.
- Identifica brand, target e perimetro editoriale.
- Cerca articoli simili recenti per evitare duplicati o angle saturi.

### 2. Discovery editoriale

- **Topic balance**: il tema e' sovra-rappresentato?
- **Verticale del brand**: il topic e' nel perimetro?
- **News value**: qual e' il trigger temporale o fattuale?
- **Distintivita**: cosa diciamo noi che altri non dicono?

### 3. Produzione spec

- Scrivi angle e struttura.
- Elenca fonti verificabili.
- Definisci take-away e vincoli di voce.

## Esiti

| Esito | Quando |
|---|---|
| `DONE` | Spec pronta per fact-check e draft. |
| `NEEDS_INPUT` | Mancano brand, target, fonte o decisione editoriale. |
| `REQUEST_CHANGES` | Topic duplicato, sbilanciato o fuori perimetro ma recuperabile. |
| `REJECTED` | Topic non verificabile, insicuro o gravemente off-brand. |

## Vincoli

- Non scrivere la bozza completa.
- Ogni H2 deve avere funzione chiara.
- Il take-away deve essere azionabile.
- Non inventare fonti o claim.

## Vedi anche

- `fact-check`
- `editorial-review`
