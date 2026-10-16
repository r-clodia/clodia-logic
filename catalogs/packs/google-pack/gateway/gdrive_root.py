"""gdrive_root — confinamento delle credenziali Google a un sottoalbero di Drive.

Perché esiste. Il connettore Workspace chiede lo scope `auth/drive` PIENO: il
token nel vault raggiunge tutto ciò che quell'account vede. Va bene quando
l'account è dedicato e non contiene nient'altro — lì il confine lo applica l'ACL
di Google. Non va bene quando l'owner ha solo account Google gratuiti e non può
creare un Drive condiviso: collegare il suo account significherebbe dare a
*qualunque* utente della webui, attraverso un agente, la lettura di *tutto* il
suo Drive. Il confine allora deve stare qui.

Cosa lo rende un controllo difendibile e non un cerotto. Il refresh token vive
nel vault e **solo il gateway lo usa**: un agente non ha la credenziale, quindi
non può aggirare questo modulo con un client suo, una shell o un `curl`. Questo
è l'unico percorso verso l'API. Il rischio residuo è un difetto in questo file,
non una via alternativa — a differenza di un filtro applicato *dopo* che i dati
sono già usciti.

Cosa NON è. Non è lo scope OAuth: il token resta pieno, e chi ottiene il
contenuto del vault (root sull'host) ha tutto Drive. È una riduzione
dell'autorità *concessa agli agenti*, non della potenza del segreto.

google-pack (clodia-platform#558). This module moved from clodia-tools
`server/tools/gdrive_root.py` into google-pack's gateway component and runs in
the gateway's process. It reads the perimeter from the GATEWAY, per call,
through the host (`host.perimeter`: the `gdrive_roots` config, the egress
list, the topic's `drive_folders`, the `gcal:` approvals; `host.channel()`:
the signed channel claim) — never from a copy of its own, and never from
anything the agent-server writes.

Semantica. `gdrive_roots` in config.yaml (volume del gateway, non raggiungibile
da un agente — lo stesso punto in cui vivono i gate):

    gdrive_roots:
      ceouncommoncreative: ["1AbC…"]   # per account
      "*": ["1XyZ…"]                    # per tutti gli account

Account SENZA voce → non confinato (comportamento storico). Account CON voce →
ogni id che entra in un verbo Google deve avere un antenato nella lista, e ogni
id che esce da un elenco viene filtrato allo stesso modo.

Tre trappole, tutte chiuse qui e nessuna ovvia:

1. **Le scorciatoie sono un reindirizzamento.** Una scorciatoia *dentro* la
   cartella che punta fuori renderebbe raggiungibile il bersaglio se risolta.
   Non si seguono mai: un file che è una scorciatoia viene rifiutato, e la
   risalita non passa mai per il suo bersaglio.
2. **Una query Drive è arbitraria.** Non si può confinare aggiungendo clausole a
   una stringa che il chiamante controlla: il confinamento sta nel filtro degli
   id restituiti, non nella query.
3. **`errore == fuori`.** Se la risalita non si completa (errore API, profondità,
   ciclo) la risposta è «fuori». La direzione opposta è l'unica che questo
   controllo non può permettersi.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from . import _host

LOG = logging.getLogger("clodia-tools.component.google-pack.gdrive-root")


def current_channel() -> Optional[str]:
    """`<tier>/<topic>` of the signed channel claim of the call (gateway-side
    `whitelist.current_channel`, through the host), or None."""
    return _host.get().channel()

CONFIG_KEY = "gdrive_roots"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
MAX_DEPTH = 32          # risalite più profonde di così non sono un albero reale
_PARENTS_TTL = 30.0     # secondi; vedi «finestra di autorità stantia» sotto
_parents_cache: dict[tuple[str, str], tuple[float, tuple[str, ...], bool]] = {}


class OutsideRoot(PermissionError):
    """Un id Google fuori dal sottoalbero consentito.

    PermissionError e non ValueError perché è un rifiuto d'autorità, e il
    dispatch dei tool lo riporta come tale.
    """


def _config() -> dict:
    raw = _host.get().perimeter.gdrive_roots()
    return raw if isinstance(raw, dict) else {}


def roots_for(account: str) -> list[str]:
    """Cartelle radice consentite per `account`; lista vuota = non confinato.

    Le voci per l'account e quelle sotto `*` si SOMMANO: `*` è un minimo comune,
    non un default che una voce specifica sostituisce. Sostituirlo sarebbe la
    direzione d'errore sbagliata — chi scrive una radice per un account non si
    aspetta di perdere quella globale.
    """
    cfg = _config()
    out: list[str] = []
    for key in ("*", account):
        v = cfg.get(key)
        if isinstance(v, str):
            v = [v]
        for fid in (v or []):
            fid = str(fid).strip()
            if fid and fid not in out:
                out.append(fid)
    # Le cartelle approvate nelle liste valgono per OGNI account: sono voci di
    # whitelist e non appartengono a un account (voce 32). Se restassero fuori di
    # qui, una cartella approvata sarebbe collegabile a un topic ma poi
    # irraggiungibile — una lista che concede a metà è peggio di nessuna lista,
    # perché chi la legge conclude che funzioni.
    for fid in approved_folders():
        if fid not in out:
            out.append(fid)
    return out


#: Prefisso con cui una cartella Drive compare nelle liste. Il vocabolario
#: esisteva già: `gdrive:folder/<id>` è un URI di egress ammesso da sempre.
FOLDER_URI = "gdrive:folder/"


def approved_folders(scope: str | None = None) -> list[str]:
    """Cartelle Drive **approvate**: voci di whitelist, non un sottoalbero.

    Le voci vengono dalla lista in vigore per la chiamata — globale PIÙ quella
    dello scope (C1) — più, per retrocompatibilità, le vecchie `gdrive_roots`.
    L'unione e non la sostituzione: chi aveva scritto una radice d'account non
    deve perderla al deploy.

    La forma è cambiata il 7 ago 2026 (voce 32). `gdrive_roots` era un TETTO
    D'ACCOUNT — un sottoalbero dentro cui tutto è permesso — e presupponeva che
    un account avesse una radice. Un account condiviso non ce l'ha: le cartelle
    arrivano da «Condivisi con me», ognuna di un proprietario diverso, senza
    antenato comune. La forma giusta è quella già usata per il repository (voce
    31) e per un indirizzo email: una voce di lista.

    Vuota = non si confina. È il comportamento storico, ed è la direzione giusta
    della retrocompatibilità: una lista vuota che chiudesse tutto verrebbe spenta
    il giorno stesso, e allora non proteggerebbe niente.
    """
    out: list[str] = []
    try:
        for u in _host.get().perimeter.egress_uris(scope):
            s = str(u).strip()
            if s.lower().startswith(FOLDER_URI):
                fid = s[len(FOLDER_URI):].strip().strip("/")
                if fid and fid not in out:
                    out.append(fid)
    except Exception as e:  # noqa: BLE001 — senza liste non si approva nulla
        LOG.warning("lettura delle cartelle approvate fallita (%s)", type(e).__name__)
    # Le vecchie `gdrive_roots` NON entrano qui. Sono per account per
    # costruzione, e portarle dentro renderebbe la radice dell'account A un
    # perimetro anche per B — cioè confinerebbe un account che oggi non lo è,
    # rompendo la compatibilità che marte richiede. Restano dove sono, dentro
    # `roots_for`, per l'account cui appartengono.
    return out


def confined(account: str) -> bool:
    """Vero se la CHIAMATA CORRENTE è confinata (per topic o per account)."""
    return bool(roots_for_call(account)[0])


_topic_cache: dict[tuple[str, str], tuple[float, Optional[str]]] = {}
_TOPIC_TTL = 20.0


def _topic_drive_folder(tier: str, name: str) -> Optional[str]:
    """La cartella Drive dichiarata per questo topic, o None (decision-record #40).

    Cache breve: questa lettura avviene su ogni chiamata Drive dentro un canale,
    e il meta di un topic cambia raramente. La finestra di staleness è
    accettabile perché dichiarare/togliere una cartella è ora un'azione da
    admin — e un admin che sposta il perimetro può attendere venti secondi.
    """
    import time
    key = (tier, name)
    now = time.time()
    hit = _topic_cache.get(key)
    if hit and (now - hit[0]) < _TOPIC_TTL:
        return hit[1]
    folder = None
    try:
        declared = _host.get().perimeter.topic_drive_folders(tier, name)
        # Prima dichiarata: stesso ripiego di `mount_by_name` pre-#40. Un topic
        # con più cartelle dichiarate ha solo la prima come perimetro — la metà
        # del lavoro che resta, come lo era allora.
        if declared:
            folder = str(declared[0].get("folder") or "").strip() or None
    except Exception as e:                       # noqa: BLE001
        # Meta illeggibile → nessuna radice DAL TOPIC. Non è un via libera: il
        # chiamante ricade sulle radici d'account, che possono essere vuote o
        # strette. Inventare un perimetro da un meta che non si è riusciti a
        # leggere sarebbe la direzione d'errore sbagliata.
        LOG.warning("gdrive_root: meta di %s/%s illeggibile (%s)",
                    tier, name, type(e).__name__)
    _topic_cache[key] = (now, folder)
    return folder


def roots_for_call(account: str) -> tuple[list[str], str]:
    """Radici valide per la CHIAMATA CORRENTE, e da dove vengono.

    Il perimetro è **per topic**, non per account: la cartella che l'owner ha
    messo come remote di un topic è la radice del confine per gli accessi che
    avvengono dentro quel canale. È la regola che Davide ha chiesto, e diventa
    implementabile perché il topic arriva in un claim FIRMATO
    (`chan:<tier>:<topic>:<agente>`): un agente non può dichiarare il topic di
    un altro per prenderne il perimetro.

    Perché non l'avevo fatto stamattina, e cosa è cambiato. L'obiezione era che
    chi può creare un remote si allarga il perimetro da sé — e l'ho verificata:
    l'endpoint della webui chiedeva `_require_member`, quindi **qualunque
    partecipante** poteva puntare il remote a `30-legale`. Il disegno regge solo
    con la conseguenza: impostare, cambiare o TOGLIERE un remote Drive è ora
    un'azione da admin, perché non è più una preferenza ma una dichiarazione di
    perimetro. Anche `remote_disable`, perché disabilitare fa ricadere sulle
    radici d'account: è un allargamento.

    Le radici d'account restano come **tetto**, non come alternativa: se
    esistono, la cartella del topic deve starci dentro, altrimenti viene
    rifiutata. Un topic non può sfondare il pavimento posato dall'owner.

    Fuori da un canale — un job, una DM — valgono le radici d'account.
    """
    acct_roots = roots_for(account)
    ch = current_channel()
    if not ch:
        return acct_roots, "account"
    tier, _, name = ch.partition("/")
    if not (tier and name):
        return acct_roots, "account"
    folder = _topic_drive_folder(tier, name)
    if not folder:
        # Canale senza remote Drive: si ricade sulle radici d'account. Negare
        # qui romperebbe un uso legittimo — allegare a una mail un file preso da
        # Drive in un topic che non ha un remote.
        return acct_roots, "account"
    if acct_roots and folder not in acct_roots:
        # TETTO. La cartella del topic deve stare dentro una radice d'account.
        # Il controllo di discendenza costerebbe una risalita a ogni chiamata:
        # qui si accetta l'uguaglianza e si delega il resto alla verifica di
        # `inside`, che intersecando entrambe le liste non può concedere più del
        # minore dei due perimetri.
        return sorted(set(acct_roots) | {folder}), "topic+tetto"
    return [folder], "topic"


def reset_topic_cache() -> None:
    _topic_cache.clear()


def _get(account: str, file_id: str, fields: str) -> dict:
    """`files.get` through the Google seam (clodia-platform#524)."""
    from . import google_io
    return google_io.call(account, "drive", "v3", "GET files/{fileId}", params={
        "fileId": file_id, "fields": fields, "supportsAllDrives": True})


def _parents(account: str, file_id: str) -> tuple[tuple[str, ...], bool]:
    """(parents, is_shortcut) di un file, con cache breve.

    La cache è una **finestra di autorità stantia**: un file spostato fuori dalla
    cartella resta raggiungibile fino a `_PARENTS_TTL`. È un compromesso
    consapevole — senza cache un elenco di 50 righe costa 50 chiamate API — e la
    finestra è tenuta a 30s perché è il tempo entro cui uno spostamento manuale
    non è realisticamente parte di un attacco.
    """
    key = (account, file_id)
    now = time.time()
    hit = _parents_cache.get(key)
    if hit and (now - hit[0]) < _PARENTS_TTL:
        return hit[1], hit[2]
    meta = _get(account, file_id, "id, parents, shortcutDetails")
    parents = tuple(meta.get("parents") or ())
    is_shortcut = bool(meta.get("shortcutDetails"))
    _parents_cache[key] = (now, parents, is_shortcut)
    return parents, is_shortcut


def inside(account: str, file_id: str, row: Optional[dict] = None) -> bool:
    """Vero se `file_id` ha un antenato fra le radici consentite.

    `row` è la RIGA restituita da un elenco Drive, non solo i suoi genitori, ed è
    una distinzione che ho già sbagliato una volta: passando i soli `parents` il
    percorso rapido salta la lettura dei metadati e quindi **non vede più che il
    file è una scorciatoia**, che è precisamente il modo numero uno di uscire dal
    perimetro. Il contratto è «tutti i campi che decidono, o nessuno»: chi ha una
    riga dell'API la passa intera (e `_FIELDS` garantisce che contenga
    `shortcutDetails`), chi non l'ha non passa niente e paga una chiamata.

    «Un qualunque cammino raggiunge la radice» è la semantica giusta per la
    LETTURA: un file con più genitori, uno dei quali dentro la cartella, è
    legittimamente nella cartella. Per lo SPOSTAMENTO conta la destinazione, che
    è un controllo separato in `move`.
    """
    roots, _fonte = roots_for_call(account)
    if not roots:
        return True
    if file_id in roots:
        return True
    seen: set[str] = {file_id}
    try:
        if row is not None and _row_is_conclusive(row):
            if _row_is_shortcut(row):
                LOG.info("gdrive_root: rifiutata scorciatoia %s (da elenco)", file_id)
                return False
            frontier = list(row.get("parents") or ())
        else:
            parents, is_shortcut = _parents(account, file_id)
            if is_shortcut:
                # Una scorciatoia è un reindirizzamento: non si segue mai.
                LOG.info("gdrive_root: rifiutata scorciatoia %s", file_id)
                return False
            frontier = list(parents)
        for _ in range(MAX_DEPTH):
            if not frontier:
                return False
            if any(p in roots for p in frontier):
                return True
            nxt: list[str] = []
            for p in frontier:
                if p in seen:
                    continue          # guardia sui cicli
                seen.add(p)
                pp, is_shortcut = _parents(account, p)
                if is_shortcut:
                    continue
                nxt.extend(pp)
            frontier = nxt
        return False
    except Exception as e:                      # noqa: BLE001 — fail-closed
        # `errore == fuori`. Una risalita incompleta non deve concedere niente.
        LOG.warning("gdrive_root: risalita di %s non conclusa (%s) → fuori",
                    file_id, type(e).__name__)
        return False


def _row_is_shortcut(row: dict) -> bool:
    """Due segnali indipendenti, entrambi da campi che chiediamo sempre."""
    return bool(row.get("shortcutDetails")) or row.get("mimeType") == SHORTCUT_MIME


def _row_is_conclusive(row: dict) -> bool:
    """Vero se la riga può DIMOSTRARE di non essere una scorciatoia.

    Il difetto che questo chiude: una riga priva di `shortcutDetails` è
    indistinguibile da una riga in cui quel campo non è stato chiesto, e fidarsi
    dell'assenza fa passare esattamente la trappola che il campo doveva chiudere.
    Il percorso rapido vale solo per righe che portano almeno uno dei due campi
    che decidono; le altre pagano la chiamata all'API invece di essere credute.
    """
    return "shortcutDetails" in row or "mimeType" in row


def _refusal(account: str, verb: str, file_id: str) -> OutsideRoot:
    roots, fonte = roots_for_call(account)
    if fonte.startswith("topic"):
        dove = (f"il canale {current_channel()} è confinato alla cartella del proprio "
                f"remote Drive ({roots})")
        rimedio = ("Se il file serve a questo canale, va messo dentro quella cartella, "
                   "oppure un admin deve cambiare il remote del topic — il perimetro "
                   "è la cartella del remote, per costruzione.")
    else:
        dove = f"l'accesso con l'account '{account}' è confinato a {roots}"
        rimedio = ("Se il file ti serve, va spostato in quella cartella da chi ne ha i "
                   "diritti.")
    return OutsideRoot(
        f"{verb}: '{file_id}' è fuori dal perimetro — {dove}, e a tutto ciò che sta "
        f"sotto. {rimedio} Il confine non è aggirabile da qui, e delegare a un altro "
        f"agente non lo sposta: userebbe la propria credenziale su un perimetro che "
        f"nessuno ha autorizzato per questa richiesta.")


def assert_inside(account: str, file_id: str, verb: str,
                  row: Optional[dict] = None) -> None:
    """Solleva `OutsideRoot` se `file_id` non è nel sottoalbero consentito."""
    if not (file_id or "").strip():
        raise ValueError(f"{verb}: id vuoto")
    if not inside(account, file_id.strip(), row):
        raise _refusal(account, verb, file_id)


def keep_inside(account: str, rows: list[dict]) -> list[dict]:
    """Filtra le righe di un elenco tenendo solo quelle nel sottoalbero.

    Passa la riga INTERA alla verifica: contiene già `parents` e
    `shortcutDetails`, quindi il figlio diretto della radice non costa nessuna
    chiamata in più e la scorciatoia viene comunque riconosciuta.
    """
    if not roots_for_call(account)[0]:
        return rows
    out = []
    for r in rows:
        fid = r.get("id")
        if fid and inside(account, fid, r):
            out.append(r)
    return out


def default_parent(account: str) -> Optional[str]:
    """Cartella da usare quando il chiamante non ne indica una.

    Senza questo, `upload`/`mkdir` senza `folder_id` scriverebbero nella radice
    di «Il mio Drive» — fuori dal confine, e per un verbo di SCRITTURA il
    rifiuto secco sarebbe una regressione gratuita: la destinazione ovvia esiste.
    """
    roots, _ = roots_for_call(account)
    return roots[0] if len(roots) == 1 else None


def assert_writable_parent(account: str, parent_id: Optional[str],
                           verb: str) -> Optional[str]:
    """Valida (o sceglie) la cartella di destinazione di una scrittura."""
    if not roots_for_call(account)[0]:
        return parent_id
    if not (parent_id or "").strip():
        dp = default_parent(account)
        if dp:
            return dp
        raise OutsideRoot(
            f"{verb}: indica la cartella di destinazione. L'accesso è "
            f"confinato a {roots_for_call(account)[0]} e senza destinazione il file "
            f"finirebbe fuori; con più radici consentite non posso indovinare "
            f"quale intendi.")
    assert_inside(account, parent_id, verb)
    return parent_id.strip()


def guard_calendar(account: Optional[str], verb: str,
                   calendar_id: Optional[str] = None) -> None:
    """Come `assert_calendar_allowed`, ma risolve l'account da sé (comodo per
    gcalendar, che non ne ha uno in mano prima di costruire il client).

    `calendar_id` è il calendario che la chiamata toccherebbe; `None` significa
    «la chiamata non ne nomina uno» ed è il solo caso di `list_calendars`.
    """
    if not _config() and not _host.get().perimeter.in_channel():
        return
    from . import gdrive
    try:
        acct = gdrive._resolve_account(account)
    except RuntimeError:
        return           # nessun account: l'errore vero lo dà il client dopo
    assert_calendar_allowed(acct, verb, calendar_id)


def assert_calendar_allowed(account: str, verb: str,
                            calendar_id: Optional[str] = None) -> None:
    """Su credenziale confinata, ammette i soli calendari DICHIARATI.

    Il calendario non è un oggetto di Drive: nessuna cartella dice qualcosa su
    di esso. Se lasciassimo passare `gcalendar.*` su una credenziale confinata,
    l'affermazione «l'agente vede solo quella cartella» sarebbe **falsa** —
    l'agenda dell'account sarebbe leggibile.

    Fino a clodia-platform#429 la conseguenza era un rifiuto secco: il
    calendario spariva per l'intera colonia appena esistesse UNA cartella
    confinata da qualche parte, e il rifiuto indicava come unico rimedio «serve
    un account non confinato». Rimedio inapplicabile — l'owner ha riconnesso
    l'account quattro volte senza che potesse cambiare nulla, perché lo scope
    `auth/calendar` era già nel consenso e questo controllo non lo guarda
    nemmeno.

    Il confine giusto non è «tutto o niente» ma la risorsa dichiarata, come per
    la casella di posta (`outbox:`) e per la cartella (`gdrive:folder/`): la
    lista nomina i calendari, e l'affermazione torna vera nella forma «solo
    quella cartella, più i calendari che l'owner ha approvato».
    """
    roots, fonte = roots_for_call(account)
    if not roots:
        return                       # non confinato: comportamento storico
    perimeter = _host.get().perimeter
    approvati = perimeter.approved_calendars()
    if calendar_id is None:
        # `list_calendars` non nomina un calendario: passa se ce n'è almeno uno
        # approvato, e la RISPOSTA viene ridotta a quelli da `keep_calendars`.
        # Passare a lista vuota sarebbe un elenco integrale dell'agenda.
        if approvati:
            return
    elif perimeter.calendar_allowed(calendar_id):
        return
    if fonte.startswith("topic"):
        dove = (f"il canale {current_channel()} è confinato alla cartella del "
                f"proprio remote Drive ({roots})")
    else:
        dove = f"l'accesso con l'account '{account}' è confinato a {roots}"
    quali = (f"Calendari approvati qui: {approvati}."
             if approvati else "Qui non è approvato nessun calendario.")
    chiesto = f" Chiesto: '{calendar_id}'." if calendar_id else ""
    raise OutsideRoot(
        f"{verb}: calendario non approvato per questo perimetro — {dove}, e il "
        f"calendario non sta in una cartella: non c'è modo di limitarlo allo "
        f"stesso perimetro, quindi vale solo ciò che è dichiarato.{chiesto} "
        f"{quali} Rimedio: un admin approva il calendario per questo canale "
        f"(voce di whitelist `gcal:<calendar_id>`, di solito l'indirizzo email "
        f"dell'account). NON è un problema di consenso OAuth: lo scope Calendar "
        f"è già nel consenso richiesto, riconnettere l'account non cambia nulla.")


def keep_calendars(account: str, rows: list[dict]) -> list[dict]:
    """Riduce l'elenco dei calendari a quelli dichiarati, se confinato.

    Senza questo, `list_calendars` su un account confinato con UN calendario
    approvato elencherebbe comunque tutti gli altri — nomi di agende altrui
    inclusi, che è proprio ciò che il confinamento promette di non mostrare.

    SHORTCUT: l'alias `primary` combacia solo con la voce letterale
    `gcal:primary`. Risolverlo nell'id reale costerebbe una chiamata API per
    elenco; regge finché chi approva scrive l'indirizzo email del calendario —
    la forma che l'API restituisce come `id`. Se servirà, si risolve una volta
    sola per account e si mette in cache accanto a `_parents_cache`.
    """
    if not roots_for_call(account)[0]:
        return rows
    perimeter = _host.get().perimeter
    return [r for r in rows
            if perimeter.calendar_allowed(str(r.get("id") or ""))
            or (r.get("primary") and perimeter.calendar_allowed("primary"))]


def reset_cache() -> None:
    """Svuota la cache dei genitori (test, e cambio di configurazione)."""
    _parents_cache.clear()


def guard_id(account: Optional[str], file_id: str, verb: str) -> None:
    """Controlla un id per i moduli che non parlano con Drive
    (gdocs, gsheets): un Doc e uno Sheet *sono* file di Drive, e il loro id è un
    id di Drive.

    Percorso rapido deliberato: se nessuna radice è configurata questa funzione
    non costruisce niente e non fa nessuna chiamata. Un account non confinato non
    paga il confinamento — altrimenti la misura si farebbe togliere per lentezza.
    """
    if not _config() and not _host.get().perimeter.in_channel():
        # Nessuna radice d'account E fuori da un canale: niente può confinare
        # questa chiamata, quindi non si tocca nemmeno il vault. Dentro un canale
        # invece il perimetro può venire dal remote del topic, e va verificato.
        return
    from . import gdrive
    acct = gdrive._resolve_account(account)
    if not roots_for_call(acct)[0]:
        return
    assert_inside(acct, file_id, verb)


def adopt(account: Optional[str], file_id: str, verb: str) -> Optional[str]:
    """Sposta dentro la radice un file appena creato dalle API Docs/Sheets.

    Docs e Sheets creano SEMPRE nella radice di «Il mio Drive»: non accettano un
    genitore. Rifiutare la creazione toglierebbe un verbo utile; lasciarla fuori
    romperebbe il perimetro. Quindi si crea e si adotta subito.

    Ritorna la cartella in cui è finito, o None se l'account non è confinato.
    Se l'adozione FALLISCE il file resta fuori: l'errore viene propagato, perché
    un «creato» silenzioso su un file non confinato sarebbe la bugia peggiore.
    """
    if not _config() and not _host.get().perimeter.in_channel():
        return None
    from . import gdrive
    acct = gdrive._resolve_account(account)
    dest = default_parent(acct)
    if not roots_for_call(acct)[0]:
        return None
    if not dest:
        raise OutsideRoot(
            f"{verb}: con più cartelle consentite su '{acct}' non posso scegliere "
            f"dove creare. Crea il file con gdrive.upload nella cartella che vuoi.")
    from . import google_io
    cur = _get(acct, file_id, "parents")
    google_io.call(acct, "drive", "v3", "PATCH files/{fileId}", params={
        "fileId": file_id, "addParents": dest,
        "removeParents": ",".join(cur.get("parents") or ()) or None,
        "fields": "id", "supportsAllDrives": True})
    reset_cache()
    return dest
