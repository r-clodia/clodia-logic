"""Gli strumenti NATIVI del runtime, dichiarati dal seed.

I ~30 tool nativi della CLI — `Read`, `Bash`, `WebSearch`, `Agent`, la famiglia
`Cron*` — non passano dal gateway. Nessuno li aveva mai dichiarati: la blocklist
esisteva per tre kind storici e conteneva solo pattern `Bash(...)`, quindi ogni
altro agente li aveva tutti. Nessuno ha deciso che un avvocato potesse cercare sul
web; lo strumento è comparso e niente ha detto no.

## Perché la rete non è il posto dove si decide

Misurato il 12 ago 2026 dentro `clodia-personal-agent-server`, col proxy di egress
attivo e `example.com` irraggiungibile via `curl`:

    WebFetch  → example.com                    BLOCCATO ("Socket is closed")
    WebFetch  → raw.githubusercontent.com      OK        (host in allowlist)
    WebSearch → query qualunque                OK, 6 risultati

`WebFetch` lo esegue la CLI in locale, quindi passa da `HTTPS_PROXY` e la
allowlist lo arbitra. `WebSearch` **no**: nel bundle della CLI è dichiarato come
tool del server (`type:"web_search_20250305"`), quindi la ricerca la fa
l'infrastruttura del provider dentro la conversazione con `api.anthropic.com` —
che è necessariamente ammessa. Il proxy non la vede.

Due conseguenze:

1. contro un tool eseguito dal provider **nessuna policy di rete può nulla**, e
   l'unico strato che lo vede è la configurazione del runtime;
2. il contenuto che entra così **non attraversa il container**, quindi la
   piattaforma non ha nemmeno l'occasione di marcare il taint. Il gate di
   contesto è scattato cinque volte l'11 ago su letture fatte *dal gateway*; la
   stessa lettura fatta con `WebSearch` non accende niente.

## Il canale di enforcement è `disallowed_tools`, non `allowed_tools`

Anche questo misurato, e ha ribaltato il piano scritto in `agents-notebook` A9:

    claude -p … --allowed-tools WebFetch      → l'agente USA WebSearch comunque
    claude -p … --disallowed-tools WebSearch  → «WebSearch non è disponibile
                                                 in questo ambiente»

`allowed_tools` è una lista di **permessi** (cosa non chiede conferma), non un
filtro dell'insieme disponibile. Solo `disallowed_tools` toglie davvero uno
strumento. Quindi il seed dichiara un'ALLOWLIST — è la forma leggibile, e la
direzione giusta per chi legge un file — e qui si calcola la sottrazione:
`NOTI − concessi`.

## Il prezzo di questa inversione, detto invece che nascosto

Una blocklist ha la direzione d'errore sbagliata: un tool aggiunto da una release
futura della CLI è abilitato di default, in silenzio, in ogni seed. Non si può
evitare — `disallowed_tools` è l'unico interruttore — ma si può rendere
**rumoroso**: `NOTI` non è una lista riscritta a mano, è quella che la CLI
dichiara nel proprio `sdk-tools.d.ts`, che viene installato col pacchetto. Un
aggiornamento che aggiunge uno strumento fa fallire un test, invece di concedere
in silenzio.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

LOG = logging.getLogger("agent-server.native_tools")

#: Dove la CLI dichiara i propri tool. Installato col pacchetto npm.
SDK_TYPES = Path("/usr/lib/node_modules/@anthropic-ai/claude-code/sdk-tools.d.ts")

#: Ripiego se il file non c'è (sviluppo sul Mac, immagine diversa). Misurato dal
#: `sdk-tools.d.ts` della CLI 2.1.197 il 12 ago 2026. Non è una seconda verità:
#: è la fotografia di quella, e il test la confronta con il file quando esiste.
KNOWN_FALLBACK: tuple[str, ...] = (
    "Agent", "Artifact", "AskUserQuestion", "Bash", "CronCreate", "CronDelete",
    "CronList", "EnterPlanMode", "EnterWorktree", "ExitPlanMode", "ExitWorktree",
    "FileEdit", "FileRead", "FileWrite", "Glob", "Grep", "ListMcpResources",
    "Mcp", "Monitor", "NotebookEdit", "Projects", "PushNotification", "REPL",
    "ReadMcpResource", "ReadMcpResourceDir", "RemoteTrigger", "ReportFindings",
    "ScheduleWakeup", "ShowOnboardingRolePicker", "TaskCreate", "TaskGet",
    "TaskList", "TaskOutput", "TaskStop", "TaskUpdate", "TodoWrite", "WebFetch",
    "WebSearch", "Workflow",
)

#: I nomi con cui il modello vede i tre tool di file. Nel `.d.ts` sono
#: `FileRead`/`FileWrite`/`FileEdit`; all'agente arrivano come `Read`/`Write`/
#: `Edit`, e `disallowed_tools` vuole il nome che l'agente vede. Una tabella di
#: traduzione di tre righe è meno peggio di una lista che nega nomi inesistenti.
ALIAS = {"FileRead": "Read", "FileWrite": "Write", "FileEdit": "Edit"}


#: Quanto di una dichiarazione ciascun runtime sa DAVVERO applicare.
#: `None` = tutto; un insieme = solo quei nomi; assente = niente.
#:
#: `disallowed_tools` e `can_use_tool` sono opzioni del Claude Agent SDK: per tre
#: mesi `native_tools` ha ristretto un runtime su tre, in silenzio, mentre il seed
#: di un agente codex mostrava la stessa lista di quello claude — ed è così che
#: Ophelia ha cercato sul web mentre Clodia, con lo stesso identico seed, non
#: poteva. Misurato il 13 ago 2026 su terra.
#:
#: - **claude**: tutto, via `disallowed_tools` + il callback dei permessi.
#: - **opencode**: la sezione `permission` del suo config. Segnato `None` come
#:   claude perché l'universo dei tool di opencode SONO quelle chiavi: ciò che
#:   non traduciamo (`OPENCODE_UNMAPPED`) non ha un nome Claude, quindi nessun
#:   seed può negarlo e non c'è residuo da dichiarare.
#: - **codex**: `tools.web_search` è l'**unica** chiave che la CLI riconosce.
#:   Misurato con `--strict-config` su codex-cli 0.137.0: `tools.web_search`
#:   passa, `tools.shell`/`tools.apply_patch`/`tools.view_image` e altre cinque
#:   sono «unknown configuration field». Tutto il resto della dichiarazione, su
#:   codex, non è applicato. Il processo NON gira più con
#:   `--dangerously-bypass-approvals-and-sandbox` incondizionato (vedi
#:   `codex_sandbox_mode`), ma il confinamento del sandbox **non è** un diniego:
#:   `read-only` lascia al modello una shell che legge. Perciò `Bash` resta nel
#:   residuo, e questa riga resta `{"WebSearch"}`.
ENFORCEABLE: dict[str, frozenset[str] | None] = {
    "claude": None,
    "opencode": None,
    "codex": frozenset({"WebSearch"}),
}


def unenforced_denied(sdk: str | None, denied: list[str] | None) -> list[str]:
    """Cosa il seed nega e questo runtime NON toglie: il residuo, per nome.

    Serve a rispondere alla domanda che il file del seed da solo non risponde —
    «questa lista conta qualcosa?». Un elenco vuoto vuol dire che conta tutta.
    """
    copre = ENFORCEABLE.get((sdk or "claude").strip().lower(), frozenset())
    if copre is None:
        return []
    return sorted(t for t in (denied or []) if t.split("(", 1)[0] not in copre)


def union_allowed(proprio: list[str] | None,
                  pavimento: list[str] | None) -> list[str] | None:
    """I nativi concessi: quelli del seed PIÙ il pavimento dell'arciseed.

    `None` da entrambi = nessuno si è pronunciato → nessuna restrizione (la
    stessa direzione d'errore di `disallowed_for`). La regola sta qui e non nel
    runtime perché ha due lettori: `session._resolve_native_allowed`, che
    configura il processo, e `trifecta.has_shell`, che deve dire se la shell c'è
    davvero — e due copie di una regola divergono.
    """
    if proprio is None and pavimento is None:
        return None
    return sorted(set(proprio or []) | set(pavimento or []))


def tool_reachable(sdk: str | None, allowed: list[str] | set[str] | None,
                   tool: str) -> bool:
    """Lo strumento arriva DAVVERO al modello su questo runtime?

    Non basta guardare la dichiarazione: una negazione che il runtime non
    applica non toglie niente. Su codex `Bash` è nel residuo di
    `unenforced_denied`, quindi un seed che non lo concede ha comunque una
    shell — ed è il fatto che un profilo di sicurezza deve riportare, invece di
    ripetere la dichiarazione (clodia-platform#296).
    """
    negati = disallowed_for(allowed)
    if tool not in negati:
        return True
    return tool in unenforced_denied(sdk, negati)


#: I campi di `sandbox` che un seed può dichiarare. Uno per uno perché il
#: residuo si dice per CAMPO: «il sandbox non è applicato» non dice a chi legge
#: quale dei suoi elenchi è finto.
SANDBOX_FIELDS: tuple[str, ...] = (
    "allow_read", "deny_read", "allow_write",
    "allow_shell_cmds", "deny_shell_patterns",
)

#: Quali di quei campi ciascun runtime applica DAVVERO.
#: `None` = tutti; un insieme = solo quelli; assente = nessuno.
#:
#: Stessa domanda di `ENFORCEABLE` un piano più sotto: quello dice *quali
#: strumenti* il seed concede, questa *come si stringe la shell che il seed ha
#: già*. Stanno nello stesso file perché divergono se stanno in due.
#:
#: - **claude**: tutti, via `.claude/settings.local.json` — è l'unico posto dove
#:   qualcuno li traduce (`agents/workspace._build_settings_json`, chiamato solo
#:   dal layout claude; un test AST tiene ferma la premessa).
#: - **codex**: nessuno. La sezione `[permissions.<profile>]` di codex 0.149
#:   sarebbe il canale giusto, ma i campi ignoti dentro un profilo non danno
#:   errore con `--strict-config`: lo schema non è stabilibile dall'interno del
#:   container, e scriverci sopra dichiarerebbe un canale che nessuno porta
#:   (clodia-platform#204, #359). Dopo la #359 il confinamento di codex esiste,
#:   ma è di grana grossa (`read-only`/`workspace-write`/`danger-full-access`):
#:   non è questa granularità.
#: - **opencode**: nessuno, oggi. Ha una configurazione di permessi documentata,
#:   quindi è il primo candidato quando l'enforcement si porterà davvero
#:   (clodia-platform#296 punto 2) — ma finché non lo si porta, dirlo qui è la
#:   sola cosa che non mente.
SANDBOX_ENFORCED: dict[str, frozenset[str] | None] = {
    "claude": None,
    "opencode": frozenset(),
    "codex": frozenset(),
}


def sandbox_unenforced(sdk: str | None, sandbox) -> list[str]:
    """I campi che il seed DICHIARA e questo runtime non porta, per nome.

    Solo i campi non vuoti: un campo assente non racconta nulla, e nominarlo
    trasformerebbe l'avviso in rumore su ogni seed non-claude. Un elenco vuoto
    quindi vuol dire «tutto ciò che è dichiarato è applicato», non «non c'è
    niente di dichiarato».
    """
    if sandbox is None:
        return []
    copre = SANDBOX_ENFORCED.get((sdk or "claude").strip().lower(), frozenset())
    if copre is None:
        return []
    return [c for c in SANDBOX_FIELDS
            if (getattr(sandbox, c, None) or []) and c not in copre]


#: Chiave di permesso di opencode → tool(i) nativi che la governano.
#:
#: I due vocabolari non coincidono e non possono: opencode ha ~15 permessi a
#: grana grossa, la CLI di Claude ~30 strumenti. Si traduce solo ciò che ha un
#: corrispondente, e il resto (`OPENCODE_UNMAPPED`) si lascia stare: decidere
#: `lsp` o `doom_loop` a partire da una lista che non li nomina sarebbe inventare
#: una policy e attribuirla al seed.
OPENCODE_KEYS: dict[str, tuple[str, ...]] = {
    "read": ("Read",),
    "edit": ("Edit", "Write"),   # un solo permesso per write+edit+patch
    "glob": ("Glob",),
    "list": ("Glob",),
    "grep": ("Grep",),
    "bash": ("Bash",),
    "task": ("Agent",),
    "webfetch": ("WebFetch",),
    "websearch": ("WebSearch",),
    "todowrite": ("TodoWrite",),
    "skill": ("Skill",),
    "question": ("AskUserQuestion",),
}

#: Chiavi dello schema di opencode senza corrispondente nel vocabolario Claude.
#: Elencate per essere viste, non per essere usate: se un giorno una di queste
#: conta, la si traduce con una decisione, non con un default.
OPENCODE_UNMAPPED = ("lsp", "external_directory", "doom_loop")


def known_tools() -> set[str]:
    """L'insieme dei tool nativi, dalla dichiarazione della CLI se leggibile."""
    try:
        testo = SDK_TYPES.read_text(encoding="utf-8")
        blocco = re.search(r"export type ToolInput[^=]*=(.*?);", testo, re.S)
        if blocco:
            nomi = re.findall(r"\|\s*(\w+)Input", blocco.group(1))
            if nomi:
                return {ALIAS.get(n, n) for n in nomi}
    except OSError:
        pass
    LOG.info("sdk-tools.d.ts non leggibile: uso l'insieme di ripiego")
    return {ALIAS.get(n, n) for n in KNOWN_FALLBACK}


def disallowed_for(allowed: list[str] | set[str] | None) -> list[str]:
    """`NOTI − concessi`, ordinato. `None` = il seed non si pronuncia → niente.

    `None` non restringe: «non mi pronuncio» non deve togliere, o il primo seed
    non aggiornato resterebbe senza `Read`. `[]` invece è una dichiarazione e nega
    tutto ciò che è noto.

    Nota su come si combinano a monte: `_resolve_native_allowed` UNISCE la lista
    del seed col pavimento dell'arciseed, quindi per un seed reale `[]` e `None`
    finiscono entrambi sul pavimento — «solo il pavimento» e «non mi pronuncio»
    coincidono nell'effetto. La differenza qui dentro resta perché questa funzione
    riceve il risultato dell'unione, e `None` da lì significa che NESSUNO si è
    pronunciato, nemmeno l'arciseed.
    """
    if allowed is None:
        return []
    esatti, prefissi, _, _ = _granted(allowed)
    return sorted(t for t in known_tools()
                  if t not in esatti and not t.startswith(prefissi or ("\0",)))


def _granted(allowed) -> tuple[set[str], tuple[str, ...], dict[str, list[str]], set[str]]:
    """Scioglie la dichiarazione in (esatti, prefissi, pattern, nudi).

    `esatti` conta un `Bash(git:*)` come una concessione di `Bash` — è ciò che
    serve alla blocklist. `nudi` invece sono le concessioni SENZA parentesi, e
    distingue «Bash intero» da «Bash ritagliato»: una differenza che su claude
    la portano i pattern stessi, e che su opencode va tradotta a mano.

    Un pattern `Bash(git:*)` concede `Bash`: il tool non si nega per intero se il
    seed ne ammette una forma ristretta. Il ritaglio fine resta ai pattern, che è
    il posto dove il runtime lo sa fare — e il terzo valore di ritorno serve a chi
    quel ritaglio deve tradurlo (opencode).
    """
    concessi = {str(x).strip() for x in allowed if str(x).strip()}
    pattern: dict[str, list[str]] = {}
    basi: set[str] = set()
    nudi: set[str] = set()
    for c in concessi:
        base, _, resto = c.partition("(")
        basi.add(base)
        if resto:
            pattern.setdefault(base, []).append(resto.rstrip(")"))
        else:
            nudi.add(base)
    # Famiglie: `Task*` concede i sei verbi dei task, `Cron*` i tre del cron.
    # Un seed che li elencasse a uno a uno dichiarerebbe sei righe dove la
    # decisione è una — e alla prossima aggiunta ne avrebbe cinque su sei.
    prefissi = tuple(b[:-1] for b in basi if b.endswith("*") and len(b) > 1)
    esatti = {b for b in basi if not b.endswith("*")}
    return esatti, prefissi, pattern, nudi


def opencode_permission(allowed: list[str] | set[str] | None) -> dict:
    """La stessa dichiarazione, tradotta nella sezione `permission` di opencode.

    Si emettono **solo i dinieghi**, come su claude: la sottrazione è l'unico
    canale, e ciò che il seed concede resta al default del runtime invece di
    essere riscritto da noi. Una chiave non mappata non viene toccata.

    Vale lo stesso prezzo detto sopra per la blocklist: un permesso nuovo nello
    schema di opencode nasce concesso. Qui però non c'è un `.d.ts` da leggere nel
    container, quindi la tabella è a mano e `OPENCODE_UNMAPPED` tiene in vista
    quel che si è deciso di non tradurre.
    """
    if allowed is None:
        return {}
    esatti, prefissi, pattern, nudi = _granted(allowed)

    def concesso(nomi: tuple[str, ...]) -> bool:
        return any(n in esatti or n.startswith(prefissi or ("\0",)) for n in nomi)

    perm: dict = {}
    for chiave, nomi in OPENCODE_KEYS.items():
        if not concesso(nomi):
            perm[chiave] = "deny"
    # `Bash(git:*)` → una mappa pattern→azione, che è la forma che opencode
    # accetta per `bash`. `X:*` nella sintassi della CLI di Claude vuol dire «un
    # comando che comincia per X»; qui diventa il comando nudo più il glob.
    if "Bash" in pattern and "Bash" not in nudi:
        regole: dict[str, str] = {}
        for p in pattern["Bash"]:
            testa, sep, coda = p.partition(":")
            if sep and coda.strip() == "*":
                regole[testa.strip()] = "allow"
                regole[f"{testa.strip()} *"] = "allow"
            else:
                regole[p] = "allow"
        regole["*"] = "deny"
        perm["bash"] = regole
    return perm


#: Le tre modalità che codex accetta, dalla più larga alla più stretta. Sono i
#: valori di `-s`/`--sandbox` **e** del campo di config `sandbox_mode`.
CODEX_FULL_ACCESS = "danger-full-access"
CODEX_WORKSPACE_WRITE = "workspace-write"
CODEX_READ_ONLY = "read-only"


def codex_sandbox_mode(allowed: list[str] | set[str] | None) -> str:
    """La stessa dichiarazione tradotta nel solo strato che codex ha: il sandbox.

    Su codex la shell non si può TOGLIERE — `tools.shell` è «unknown
    configuration field», rimisurato con `--strict-config` su codex-cli 0.149.0
    il 25 ago 2026 (era già così su 0.137). Quello che si può fare è
    **confinarla**: `sandbox_mode` è un campo riconosciuto. Finché il processo
    partiva con `--dangerously-bypass-approvals-and-sandbox` incondizionato,
    nemmeno quel confinamento contava, e un agente il cui seed non concede
    `Bash` teneva comunque una shell senza limiti — su un runtime dove nessun
    altro strato la vede.

    La mappa, e il perché di ogni riga:

    - **nessuna dichiarazione** (`None`: non si è pronunciato nessuno, nemmeno
      l'arciseed) → `danger-full-access`. `None` non restringe, esattamente come
      in `disallowed_for`: la direzione d'errore è la stessa di tutto il file.
    - **`Bash` concesso**, nudo o ritagliato → `workspace-write`. Il ritaglio per
      comando codex non lo sa fare (`tools.shell` non esiste, e i pattern non
      hanno dove andare), quindi quel pezzo resta residuo dichiarato; la radice
      scrivibile è la cwd, che per una sessione è lo scratch dello spawn — cioè
      l'`allow_write: ["{scratch}/**"]` che i seed codex già dichiarano.
    - **niente `Bash` ma `Write`/`Edit`** → `workspace-write` comunque:
      l'`apply_patch` di codex passa dal medesimo sandbox, e `read-only` gli
      toglierebbe la scrittura che il seed CONCEDE.
    - **né shell né scrittura** → `read-only`.

    ⚠️ `read-only` non è un diniego di `Bash`: il modello può ancora leggere con
    la shell. Per questo `ENFORCEABLE["codex"]` resta `{"WebSearch"}` e `Bash`
    continua a comparire nel residuo. Il confinamento riduce il raggio; chiamarlo
    enforcement sarebbe rifare il difetto che questo file esiste per non rifare.
    """
    if allowed is None:
        return CODEX_FULL_ACCESS
    esatti, prefissi, _, _ = _granted(allowed)

    def concesso(nome: str) -> bool:
        return nome in esatti or nome.startswith(prefissi or ("\0",))

    if any(concesso(n) for n in ("Bash", "Write", "Edit")):
        return CODEX_WORKSPACE_WRITE
    return CODEX_READ_ONLY
