---
name: topic-files
description: |
  Come lavorare sui FILE di un topic/canale (specialmente binari: xlsx, pdf, docx,
  pptx, zip, immagini). I file del topic vivono dietro il gateway, NON nel tuo
  filesystem: non puoi aprirli direttamente né con path tipo `files/...`. Modello
  "come git ma non git": chiedi al gateway una COPIA nel tuo scratch (`topic.fetch`),
  la tratti in locale con le skill STANDARD (xlsx/pdf/docx/…), poi la riconsegni al
  gateway (`topic.put`). REGOLA: per i binari NON usare topic.read_file/write_file
  (passano base64 nel tuo contesto e si troncano sui file grandi → file corrotto).
  Usare ogni volta che devi leggere, creare o modificare un file di un topic.
---

# topic-files — lavorare sui file di un topic senza corromperli

## Il modello (come git, ma non git)
I file di un topic stanno nel **topic store dietro il gateway**, non sul tuo disco.
Per lavorarci:
1. **`topic.fetch`** — il gateway (verificati i permessi) mette una **copia** del
   file nel tuo **scratch** e ti ritorna il path locale. *(È come un `clone`/`pull`.)*
2. Tratti la copia **in locale** con la **skill standard** adatta (es. la skill
   `xlsx` per gli Excel) — quelle skill restano intatte e lavorano su file normali.
3. **`topic.put`** — il gateway prende il file dal tuo scratch e lo scrive nel
   topic. *(È come un `commit`/`push`.)*

I byte viaggiano **come file**, mai come base64 nel tuo contesto.

## Perché NON topic.read_file/write_file per i binari
Quei verbi mettono il contenuto come stringa nel tuo contesto. Per un testo va
bene; per un binario (xlsx/pdf/zip/immagine) il file viaggia in base64 e su file
non piccoli **si tronca** → "file corrotto / not a zip file". Per i binari usa
**sempre** `topic.fetch` + `topic.put`.

## Ricetta (esempio: compilare un template xlsx)
```bash
# 0) scopri il path del tuo scratch (la tua cwd è la cartella dello spawn)
SCR="$PWD/scratch"; mkdir -p "$SCR"
```
```text
# 1) fetch del template nel tuo scratch (path assoluto in dest)
topic.fetch(tier="SEAL-1", name="<canale>",
            path="files/expenses/template.xlsx",
            dest="<SCR>/template.xlsx")
```
```text
# 2) trattalo con la skill STANDARD xlsx sul file LOCALE <SCR>/template.xlsx,
#    compilando TUTTI i dati disponibili (vedi sotto), salva <SCR>/out.xlsx
```
```text
# 3) put nel topic
topic.put(tier="SEAL-1", name="<canale>",
          filename="expenses/out.xlsx", src="<SCR>/out.xlsx")
```
`dest`/`src` devono essere **path assoluti sotto il tuo scratch**
(`/datadir/spawns/<tuo-spawn>/scratch/...`); il gateway rifiuta path fuori.
Dopo il `put`, verifica con `topic.files` che il file sia presente con la size attesa.

## Completezza dei dati
Prima di compilare, **raccogli TUTTE le voci dal topic**, non solo quelle citate in
chat: leggi il `summary.md` e i file in `files/` (ricevute, conferme). Es. per un
rimborso: voli **e** alloggio **e** eventuali altri costi — spesso l'alloggio è nel
summary, non nei messaggi. Se un dato manca davvero, chiedi: è un documento reale.
