---
name: topic-binary-files
description: |
  Come creare/modificare file BINARI (xlsx, pdf, docx, pptx, zip, immagini) dentro
  un topic SENZA corromperli. REGOLA: un binario NON deve mai passare per il modello
  come base64 — su file grandi (es. un xlsx da ~100KB) il base64 viene troncato e il
  file risulta illeggibile. Si manipola in bash (python/openpyxl) usando i due
  endpoint localhost `fetch-file`/`put-file` dell'agent-server, che muovono i byte
  lato server. Usare OGNI volta che si deve produrre o editare un deliverable
  binario in un canale/topic (rimborsi, report, allegati). Per i file di TESTO
  (.md, .txt, .csv) va benissimo `topic.write_file` normale.
---

# topic-binary-files — deliverable binari senza corruzione

## Perché
`topic.read_file`/`topic.write_file` fanno transitare il contenuto come stringa
nel TUO contesto. Per un testo va bene. Per un BINARIO (xlsx/pdf/docx/zip/img) il
file viaggia in base64: leggerlo e poi **riemetterlo intero** come tua risposta è
impossibile su file non piccoli → il base64 si tronca → file corrotto ("Excel
cannot open / not a zip file"). **Non farlo mai.**

## Come (ricetta bash)
I byte li muove l'agent-server tramite due endpoint **solo-localhost**
(`127.0.0.1:7842`, raggiungibili solo dal tuo bash):

1. **Scarica** il file in uno scratch locale (`/tmp`), niente byte nel contesto:
   ```bash
   curl -sG http://127.0.0.1:7842/clodia/agent/fetch-file \
     --data-urlencode "tier=SEAL-1" \
     --data-urlencode "name=<canale>" \
     --data-urlencode "path=files/expenses/Travel_reimbursement_template.xlsx"
   # → {"local_path":"/tmp/clodia-agent-files/<id>.xlsx","size":97697}
   ```

2. **Modifica** in python/openpyxl (sono già installati nel container), lavorando
   solo su path locali — mai stampare il binario:
   ```bash
   python3 - <<'PY'
   import openpyxl
   wb = openpyxl.load_workbook("/tmp/clodia-agent-files/<id>.xlsx")
   ws = wb.active
   ws["B9"]  = "Davide Carboni"
   ws["C24"] = 285.86
   wb.save("/tmp/out.xlsx")
   print("ok", "/tmp/out.xlsx")
   PY
   ```

3. **Carica** passando il PATH locale (il base64 verso il gateway lo fa il server):
   ```bash
   curl -s -X POST http://127.0.0.1:7842/clodia/agent/put-file \
     -H 'Content-Type: application/json' \
     -d '{"tier":"SEAL-1","name":"<canale>","filename":"expenses/Travel_reimbursement_CARBONI.xlsx","local_path":"/tmp/out.xlsx"}'
   # → {"ok":true,"filename":"expenses/...","size":54873}
   ```

`filename` può includere sottocartelle (finiscono sotto `files/`). Dopo il
`put-file`, verifica con `topic.files` che il file sia presente con la size attesa.

## Regole
- **Binario in/out → SEMPRE fetch-file/put-file + bash.** Mai base64 nel modello.
- Tieni i file in `/tmp` (gli endpoint accettano solo `local_path` sotto `/tmp/`).
- Se un dato ti manca (es. IBAN, importi), chiedi invece di inventare: è un
  documento reale.
- Per generare un binario da zero (non da template) vale lo stesso: crealo in
  `/tmp` con python, poi `put-file`.
