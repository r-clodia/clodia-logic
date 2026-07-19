---
name: fact-check
description: |
  Verifica fonti e claim di una news, brief o articolo prima della scrittura. Produce un dossier con claim, fonti, stato verifica, perimetro e anomalie dell'input. Usa fonti primarie e cross-check minimo su due fonti per claim numerici o normativi.

  Skill di dominio trasferibile a qualunque brand. Il control plane decide come consegnare il dossier.
---

# Skill: fact-check

Verifica delle fonti per un contenuto editoriale. Senza fact-check rigoroso,
un articolo puo introdurre claim falsi o non documentati.

## Quando si applica

- Brief editoriale approvato.
- Articolo o bozza con claim da verificare.
- News pickup con fonte incerta.
- Contenuto B2B/compliance/risk dove date, numeri e norme sono critici.

## Input

- Brief o bozza.
- Angle approvato.
- URL/fonte di partenza, se presente.
- Claim numerici, normativi o fattuali da verificare.
- Brand/topic rules, se disponibili.

## Output

Dossier markdown:

```markdown
# Dossier fact-check — <titolo>

**Data**: YYYY-MM-DD
**Angle verificato**: <una riga>

## Fatti verificati

| # | Claim | Fonte primaria | Verificato |
|---|---|---|---|
| 1 | <claim> | <riferimento preciso> | OK: <fonte 1 + fonte 2> |

## Settori/perimetro coperti

<chi e' soggetto alla norma/evento/dato>

## Anomalie dell'input

- <date errate, URL 404, claim gonfiati, fonti mancanti>

## Angle editoriale verificato

- Target: <ok/note>
- Distintivita: <ok/note>
- Voice/rules: <ok/note>

## Fonti per citazione

1. [Fonte primaria](url)
2. [Fonte secondaria affidabile](url)
```

## Workflow

### 1. Carico contesto

- Estrai tutti i claim verificabili.
- Se la fonte di partenza e' rotta o insufficiente, cerca fonte primaria
  alternativa.

### 2. Verifica fonti

Per ogni claim numerico, normativo o data:

- almeno 2 fonti convergenti quando possibile;
- fonte primaria preferita;
- se fonti non convergono, claim non utilizzabile.

Preferenze:

- Cybersecurity / NIS2 / DORA: ACN, ENISA, Gazzetta Ufficiale, autorita.
- AI governance / AI Act: EU AI Office, AgID, Garante Privacy, NIST, IAPP.
- Crypto / MiCA / RWA: ESMA, EBA, BCE, Banca d'Italia.
- Privacy / GDPR: EDPB, Garante Privacy, IAPP.
- ISO: ISO/IEC e certification body affidabili.

Evita aggregatori clickbait, vendor pitch e paywall senza fonte primaria
alternativa.

### 3. Produci dossier

- Riporta claim verificati e non verificati.
- Esplicita anomalie dell'input.
- Non trasformare il dossier in bozza articolo.

## Esiti

| Esito | Quando |
|---|---|
| `DONE` | Dossier completo, claim centrali verificati. |
| `REQUEST_CHANGES` | Brief/bozza richiede correzioni per claim errati o non supportati. |
| `NEEDS_INPUT` | Mancano fonte, angle o contesto minimo. |
| `REJECTED` | Il contenuto richiede claim non documentabili o contrari a policy. |

## Vincoli

- Cita sempre le fonti.
- Non inventare claim o riferimenti.
- Ometti claim non verificabili.
- Se una fonte e' di parte, segnala il bias.

## Vedi anche

- `article-spec`
- `editorial-review`
