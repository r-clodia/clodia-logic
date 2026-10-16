---
name: topic-drive-sync
description: |
  Come sincronizzare i file di un topic con una cartella Google Drive. In astratto
  sono DUE alberi di file/cartelle: un REMOTO (source of truth, Drive) e un LOCALE
  (replica, topic). I verbi sono astratti e valgono per qualunque coppia di store,
  ma il caso primario è Drive → topic. Sync OTTIMISTICO,
  il REMOTO vince, NIENTE viene cancellato (le rimozioni vanno nel cestino), e i
  file in CONFLITTO si scalano all'umano mentre tutti gli altri si sincronizzano.
  La skill ragiona con VERBI ASTRATTI: declinali sui tool concreti del contesto
  (es. gdrive.* per il remoto, topic.* per il locale). Usare ogni volta che devi
  tenere allineata una cartella locale con una remota.
---

# topic-drive-sync — sincronizzare i file di un topic con Google Drive

## Modello astratto
Due alberi di nodi (file e cartelle), ciascuno raggiunto da un **path relativo**
a una radice:
- **REMOTO** — la **source of truth**: detta direzione e struttura.
- **LOCALE** — la **replica**: deve riflettere il remoto.

La skill **non** dipende dai tool concreti. Usa questi **verbi astratti** e
declinali sui tool disponibili nel contesto d'uso:

| Verbo astratto | Cosa fa |
|---|---|
| `enumerate(tree)` | albero ricorsivo → lista di `{path, kind: file\|dir, fingerprint}` |
| `read(tree, path)` | restituisce i byte del file |
| `write(tree, path, bytes)` | crea/sovrascrive il file (creando le cartelle intermedie) |
| `make_dir(tree, path)` | crea una cartella |
| `trash(tree, path)` | **soft-delete**: sposta nel cestino del tree (MAI elimina davvero) |
| `same(a, b)` | confronto via `fingerprint` (hash del contenuto; in mancanza, size+mtime) |

### Declinazione di riferimento (Google Drive → topic)
| Verbo | REMOTO = Google Drive | LOCALE = topic |
|---|---|---|
| enumerate | `gdrive.list` (ricorsivo per folder) | `topic.files` (ricorsivo) |
| read | `gdrive.download` → file in scratch | `topic.fetch` → file in scratch |
| write | `gdrive.upload` (+`gdrive.mkdir` per il parent) | `topic.put` (dallo scratch) |
| make_dir | `gdrive.mkdir` | implicito in `topic.put` |
| trash | cestino di Drive (o, se non disponibile, **segnala** invece di rimuovere) | `topic.delete_file` (→ `.trash/`) |
| same | campo `md5` di `gdrive.list` | campo `md5` di `topic.files` |

*(Il trasporto dei byte passa per lo **scratch** dell'agent, non per il contesto:
niente base64 di file grandi. Vedi skill [[topic-files]].)*

## Algoritmo (ottimistico, remoto = verità)
1. **Enumera** REMOTO e LOCALE (ricorsivi) → due mappe `path → nodo`.
2. **Allinea le cartelle**: ogni cartella presente nel REMOTO e assente nel LOCALE
   → `make_dir` nel LOCALE.
3. **Classifica ogni file** per path:
   - **solo REMOTO** → `write` nel LOCALE (pull).
   - **solo LOCALE** → `trash` nel LOCALE (il remoto è verità; soft-delete →
     recuperabile dal cestino).
   - **in entrambi, `same`** → niente.
   - **in entrambi, divergenti** → **CONFLITTO**: NON toccare nessuno dei due,
     accoda alla lista conflitti.
4. **Esegui** tutte le azioni non-conflittuali. È **ottimistico** e **per-file**:
   nessun lock; un errore su un file **non blocca** gli altri (registralo e prosegui).
5. **Conflitti → umano (granulare)**: a fine sync, se la lista conflitti non è
   vuota, **scala all'umano SOLO per quei file** (path + natura della divergenza),
   proponendo per ciascuno: *tieni remoto* · *tieni locale* · *tieni entrambi
   (rinomina)*. **Tutto il resto è già sincronizzato** — non si aspetta la
   risoluzione dei conflitti per propagare i file puliti.

## Principi (non negoziabili)
- **Ottimistico**: procede assumendo il successo, isola i fallimenti per-file,
  ed è **idempotente** (ri-eseguirla converge senza danni).
- **Remoto = source of truth**: direzione e struttura le detta il remoto.
- **Niente si cancella mai**: ogni rimozione è un `trash` (cestino), recuperabile.
- **Conflitto ≠ sovrascrittura cieca**: anche se il remoto è "verità", un file
  divergente su entrambi i lati può contenere lavoro locale non ancora propagato
  al remoto → si **scala**, non si sovrascrive. La preferenza per il remoto vale
  per i casi non ambigui (solo-remoto, struttura), non per dirimere i conflitti.
- **Granularità**: i conflitti bloccano **solo sé stessi**, mai l'intero sync.

## Come riconoscere un conflitto (stateless)
Stesso path presente su entrambi i lati con `fingerprint` **diverso** → conflitto.
La skill è **stateless per scelta**: non conserva uno stato dell'ultima sync,
quindi non può sapere *chi* ha cambiato → ogni divergenza di contenuto è un
**conflitto prudente** che si scala, mai una sovrascrittura cieca (potrebbe
esserci lavoro locale da preservare). *(Una baseline persistita per distinguere
"solo remoto cambiato" da "entrambi cambiati" e ridurre i falsi conflitti è una
possibile evoluzione futura, fuori scope ora.)*

## Report finale
Riassumi sempre: cartelle create, file scaricati/aggiornati nel locale, file
cestinati, file **in conflitto** (elenco con path), e gli eventuali errori
per-file. Così l'umano vede cosa è stato fatto e cosa deve dirimere.
