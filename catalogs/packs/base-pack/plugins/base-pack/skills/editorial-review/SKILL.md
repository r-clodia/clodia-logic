---
name: editorial-review
description: |
  Quality review di una bozza o articolo pubblicabile. Verifica voce, fonti, lunghezza, take-away, target, tribalismo e coerenza col fact-check. Emette verdetti APPROVED, REVISE_INLINE, REVISE_RETURN o REJECTED.

  Skill di dominio trasferibile a qualunque brand. Il control plane decide come instradare il verdict; la skill produce review e, se richiesto, fix inline limitati.
---

# Skill: editorial-review

Review editoriale di una bozza o articolo.

## Quando si applica

- Bozza articolo pronta per review.
- Preview/articolo integrato da validare.
- Serve verificare qualita editoriale e coerenza fact-check.

## Input

- Bozza markdown, HTML/TSX o preview.
- Dossier `fact-check`.
- Brief/spec articolo.
- Rules di brand e corpus recente, se disponibili.

## Output

- Checklist 7 dimensioni.
- Verdict: `APPROVED`, `REVISE_INLINE`, `REVISE_RETURN`, `REJECTED`.
- Issue puntuali con fix suggerito.
- Eventuali fix inline se piccoli e autorizzati.

## Verdict

| Verdetto | Quando |
|---|---|
| `APPROVED` | Tutte le dimensioni passano. |
| `REVISE_INLINE` | 1-3 fix micro, <=5 righe, zero cambi struttura. |
| `REVISE_RETURN` | Cambi sostanziali a angle, titolo, struttura, voce o claim. |
| `REJECTED` | Off-brand grave, claim non verificabili, policy violata. |

## Checklist

| Dimensione | PASS se | FAIL se |
|---|---|---|
| Voce | Coerente con brand, lead concreto | Buzzword, marketese, introduzioni vaghe |
| Fonti | Citazioni a fonte primaria/affidabile | Claim senza fonte |
| Lunghezza | Nel range richiesto | Troppo corto/lungo senza ragione |
| Take-away | Azionabile e specifico | Generico o assente |
| Target | Allineato alla persona prevista | Scritto per pubblico sbagliato |
| Tribalismo | Posizioni bilanciate | Tifo o framing ideologico non richiesto |
| Fact-check | Numeri/date/norme coerenti col dossier | Contraddice o inventa claim |

## Workflow

### 1. Carico contesto

- Leggi bozza, brief e dossier.
- Carica rules di brand.
- Se ci sono calcoli o percentuali, verifica con calcolatrice.

### 2. Applica checklist

- Assegna PASS/FAIL con note.
- Distingui typo/fix puntuali da problemi strutturali.

### 3. Produci review

```markdown
verdict: APPROVED | REVISE_INLINE | REVISE_RETURN | REJECTED

## Checklist
| Dimensione | Esito | Note |
|---|---|---|

## Issue
- <problema + fix suggerito>

## Verdetto
<motivazione>
```

## Esiti

| Esito skill | Quando |
|---|---|
| `DONE` | Review completata e verdict emesso. |
| `REQUEST_CHANGES` | Verdict `REVISE_INLINE` o `REVISE_RETURN`. |
| `REJECTED` | Verdict `REJECTED`. |
| `NEEDS_INPUT` | Mancano bozza, fact-check o brief. |

## Vincoli

- Verifica sempre calcoli matematici.
- Fonti solo aggregatori: FAIL.
- Se superi 5 righe di fix, non e' piu inline.
- Non rifare fact-check completo; segnala inconsistenze al producer del dossier.

## Vedi anche

- `fact-check`
- `article-spec`
- Rules di brand, es. `acme-blog-voice`
