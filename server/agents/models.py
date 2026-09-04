"""Pydantic models per agent.yaml.

Lo schema riflette esattamente i 4 prototipi validati nel topic
`acme-blog-agents/files/agents-proto/` (29 mag 2026).
"""
from __future__ import annotations
from typing import Literal, Optional

from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator


AgentType = Literal["bot", "human", "proxy"]
#: Ciò che un proxy può dichiarare: parla e legge LA SUA stanza, e basta.
#:
#: Fino al 14 ago 2026 qui c'era il solo `topic.post_message`, e il token che il
#: gateway coniava per un proxy ne portava dieci — quelli di una persona. Il
#: seed prometteva una cosa e la credenziale ne consegnava un'altra, che è il
#: modo in cui una restrizione smette di essere una restrizione.
#:
#: Deve restare allineato a `PROXY_VERBS` in `human_mcp.py` (clodia-tools):
#: questo è ciò che si dichiara, quello è ciò che si conia.
_PROXY_ALLOWED_TOOLS = {
    "topic.post_message",   # scrive nel canale (e menziona: la menzione è testo)
    "topic.messages",       # legge il canale — senza, non dialoga: è un webhook
    "topic.my_mentions",    # sa di essere stato chiamato
    "topic.mark_seen",
}


def normalize_agent_type(value: object) -> str:
    """Canonical agent vocabulary.

    `normal` and `super` are accepted on read as legacy seed/API values, but the
    registry stores and emits the single executable-agent type: `bot`.
    """
    raw = str(value or "bot").strip().lower()
    if raw in {"normal", "super"}:
        return "bot"
    return raw


class Sandbox(BaseModel):
    """Permessi applicati al workspace effimero via .claude/settings.json.

    Tutti i path sono relativi alla data root (`/clodia` nel container,
    `WORKSPACE_ROOT` localmente). Il placeholder `{scratch}` è risolto
    runtime al path dello scratch dell'istanza.

    ⚠️ **Solo il runtime claude li applica.** Su codex e opencode nessuno li
    traduce: chi li dichiara lì scrive una restrizione che non esiste a runtime.
    Quali campi ciascun runtime porta sta in `sdk_runtime.native_tools`
    (`SANDBOX_ENFORCED`), e ciò che non porta esce come avviso del loader e nel
    `sandbox_info.unenforced` della scheda (clodia-platform#296).
    """
    model_config = ConfigDict(extra="forbid")

    allow_read: list[str] = Field(default_factory=list)
    deny_read: list[str] = Field(default_factory=list)
    allow_write: list[str] = Field(default_factory=list)
    allow_shell_cmds: list[str] = Field(default_factory=list)
    deny_shell_patterns: list[str] = Field(default_factory=list)


class MemoryConfig(BaseModel):
    """Memory persistente dell'agente. None = stateless (no memory)."""
    model_config = ConfigDict(extra="forbid")

    dir: str = Field("memory/", description="Path relativo alla cartella agente")


class OnCompleteAction(BaseModel):
    """Azione dichiarativa (deprecated nel modello inbox v3, mantenuto
    per backward-compat — gli handoff sono dinamici via scratch/handoff.json)."""
    model_config = ConfigDict(extra="forbid")

    action: str
    files: Optional[list[str]] = None
    template: Optional[str] = None
    to_list: Optional[str] = None


class OutputsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifacts: list[str] = Field(default_factory=list)
    on_complete: list[OnCompleteAction] = Field(default_factory=list)


class PluginRequirement(BaseModel):
    """Prerequisito di un agent seed verso un plugin del catalogo.

    Soft (`hard: false`, default): l'agente parte anche senza il plugin, in
    modalità degradata; l'API packs espone il warning. `hard: true` è solo
    dichiarativo per ora (nessun enforcement al boot)."""
    model_config = ConfigDict(extra="forbid")

    name: str
    hard: bool = False


class StackSpec(BaseModel):
    """Uno stack di inferenza dell'agente: la tupla (LLM, provider).

    Modello concettuale "1 seed → N stack" (issue clodia-platform#93): il
    modello NON è più una proprietà fissa dell'agente — è una proprietà dello
    stack. L'ordine degli stack nel seed è la preferenza; a runtime è attivo
    uno stack alla volta (il primo col provider connesso e non in pausa,
    salvo override manuale dal profilo).
    """
    model_config = ConfigDict(extra="forbid")

    model: str
    provider: str


class AgentSpec(BaseModel):
    """Specifica completa di un agente caricata da agent.yaml.

    Mantiene path al system_prompt e alle skill come stringhe relative —
    il loader li risolve al filesystem dopo il parse, per consentire al
    workspace effimero di copiare i file senza ulteriori indirezioni.
    """
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    # Frase-dominio concisa (1-2 righe) per il ROUTING del risponditore per
    # rilevanza: descrive i compiti/temi che l'agente presidia (es. "Analisi di
    # documenti lunghi, minute, preventivi"). Se vuota, il routing ripiega su
    # description+capabilities (segnale più debole). Vedi responder_routing.
    expertise: str = ""
    # Modello d'inferenza. Obbligatorio per gli agent ESEGUITI (`bot`);
    # None per i principal `human` (non eseguiti: nessun motore).
    model: Optional[str] = None
    display_name: str
    avatar_color: str = "#888888"

    # Categoria identità: `bot` (seed eseguibile), `human` (principal umano) o
    # `proxy` (sistema terzo ammesso in uno scope, non eseguito).
    # Legacy accettati in lettura: `normal` e `super` -> `bot`.
    type: AgentType = "bot"

    @field_validator("type", mode="before")
    @classmethod
    def _coerce_type(cls, v):
        return normalize_agent_type(v)

    # Genealogia del seed (modello ereditario): progenitore/i da cui questo seed
    # discende. Rende tracciabile il "drift" delle costituzioni dal genoma di
    # Clodia Primal. Es. ["clodia-primal"]. Vuoto = capostipite.
    parents: list[str] = Field(default_factory=list)

    # Seed ASTRATTO: esiste per essere ereditato, non per essere spawnato
    # (specification §1.4). Dichiararlo non basta — va imposto al momento dello
    # spawn, perché un seed astratto materializzato per errore è un agente coi
    # soli verbi base e nessun mestiere: funziona abbastanza da non farsene
    # accorgere, e poi risponde male senza che nulla lo segnali.
    abstract: bool = False

    # Il seed dichiara di poter servire ogni tier usato dall'istanza. Non basta
    # che oggi il suo stack effettivo arrivi in alto: quel fatto operativo può
    # cambiare con un override/provider, mentre questo è un contratto del ruolo.
    all_tier: bool = False

    # Riferimento alla costituzione (genoma) fuso in testa al system prompt al
    # render. Risolto da constitution-catalog/<ref>.md (data-over-logic). None
    # o "none" = nessuna costituzione (es. worker minimali). Es. "platform-core".
    constitution: Optional[str] = None

    # SDK di esecuzione: "claude" | "codex" | "opencode". Default "claude"
    # (fix del default mancante, test test_legacy_agent_defaults_to_claude).
    agent_sdk: str = "claude"

    # Provider delle credenziali che alimentano il modello — completa lo stack
    # agent → model → provider. Es. "anthropic" | "openai" (catalog in
    # api/providers.py). None = derivato dall'`agent_sdk` (claude→anthropic,
    # codex→openai) dal resolver. Dichiararlo esplicito serve quando lo stesso
    # SDK ha più provider/account possibili. Se il provider non è collegato,
    # l'agent appare "disconnected" nella webui.
    # DEPRECATO dallo split provider (21 giu 2026): usare `providers` (lista
    # ordinata). Mantenuto per back-compat: se `providers` è vuoto e questo è
    # valorizzato, vale come lista a un elemento.
    provider: Optional[str] = None

    # Provider di inferenza COMPATIBILI, in ordine di preferenza. A runtime si
    # sceglie il PRIMO collegato; se NESSUNO è collegato l'agent resta
    # disattivato. Es. ["anthropic-api", "claude-pro-max"] = preferisci l'API
    # (DPA commerciale), ripiega sull'abbonamento. Vuoto = default dell'SDK.
    providers: list[str] = Field(default_factory=list)

    # Override del MODELLO per-provider: consente una catena di fallback con
    # provider che servono modelli (e SDK) diversi. Chiave = id provider, valore =
    # modello da usare con quel provider. L'SDK del runtime segue quello del
    # provider (catalog), NON questo campo. Es. {"aws-region-eu": "claude-haiku-4-5"}
    # su un agent con model top-level "gpt-oss-120b" (scaleway/opencode): primario
    # gpt-oss su scaleway, fallback haiku su Bedrock. I provider NON elencati usano
    # il `model` top-level. Vuoto = un solo model per tutti i provider (back-compat).
    provider_models: dict[str, str] = Field(default_factory=dict)

    # ── Stack di inferenza (issue clodia-platform#93): 1 seed → N stack ────
    # Sintassi PRIMARIA per dichiarare le tuple (model, provider), in ordine
    # di preferenza. `model`/`providers`/`provider_models` restano come zucchero
    # legacy: `_normalize_stacks` normalizza nelle due direzioni, così tutto il
    # runtime (candidate/effective_provider, _runtime_model, catene cross-SDK)
    # lavora sui campi derivati senza conoscere gli stack.
    # Vincolo v1: un provider compare al massimo in UNO stack (l'identità
    # della selezione runtime — override, per-tier, telemetria — è il
    # provider id). Stack multipli sullo stesso provider = follow-up.
    stacks: list[StackSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_stacks(self):
        """Normalizza stacks ⇄ legacy (model/providers/provider_models)."""
        if self.stacks:
            seen: set[str] = set()
            for s in self.stacks:
                if s.provider in seen:
                    raise ValueError(
                        f"stacks: provider duplicato '{s.provider}' — v1 ammette "
                        "al massimo uno stack per provider")
                seen.add(s.provider)
            if self.providers or self.provider_models:
                # stacks vince; i legacy dichiarati insieme sono ignorati.
                # Warning (non errore): un seed ridondante non deve impedire
                # il boot dell'agente.
                import logging
                logging.getLogger("agent-server.agents.models").warning(
                    "agent %s: dichiarati sia `stacks` che providers/"
                    "provider_models legacy — stacks vince, legacy ignorati",
                    self.name)
            primary = self.stacks[0].model
            self.model = primary
            self.providers = [s.provider for s in self.stacks]
            self.provider_models = {s.provider: s.model
                                    for s in self.stacks if s.model != primary}
        elif self.providers and self.model:
            self.stacks = [StackSpec(model=self.provider_models.get(p, self.model),
                                     provider=p) for p in self.providers]
        return self

    @model_validator(mode="after")
    def _proxy_has_no_runtime(self):
        """Un proxy parla nel topic ma non ha runtime, memoria o file propri."""
        if self.type != "proxy":
            return self
        if self.model or self.provider or self.providers or self.provider_models or self.stacks:
            raise ValueError("proxy: nessun model/provider/stack ammesso")
        if self.system_prompt:
            raise ValueError("proxy: nessun system_prompt ammesso")
        if self.memory is not None:
            raise ValueError("proxy: nessuna memory ammessa")
        sandbox = self.sandbox
        if any((
            sandbox.allow_read,
            sandbox.deny_read,
            sandbox.allow_write,
            sandbox.allow_shell_cmds,
            sandbox.deny_shell_patterns,
        )):
            raise ValueError("proxy: nessun sandbox/file access ammesso")
        if self.native_tools:
            raise ValueError("proxy: nessun native_tool ammesso")
        extra_tools = set(self.tool_permissions or []) - _PROXY_ALLOWED_TOOLS
        if extra_tools:
            raise ValueError(
                "proxy: ammessi solo " + ", ".join(sorted(_PROXY_ALLOWED_TOOLS))
                + " — non " + ", ".join(sorted(extra_tools))
            )
        if any((self.rag_read, self.rag_write, self.volumes, self.credentials, self.carries or [])):
            raise ValueError("proxy: nessun file/RAG/volume/credential ammesso")
        return self

    @model_validator(mode="after")
    def _activation_and_multi_spawn_agree(self):
        """`activation` e `multi_spawn` sono due nomi dello stesso fatto: uno
        solo va dichiarato, e i due non possono dire cose opposte (R15, #191).

        - non dichiarato → derivato da `multi_spawn` (quindi `queue` per tutti i
          seed esistenti: nessun cambiamento in produzione);
        - `parallel` dichiarato → accende `multi_spawn`, che è il campo che il
          canale legge davvero. Il seed dichiara la meccanica e la OTTIENE:
          un'etichetta che non governa il comportamento sarebbe una metà-fix;
        - contraddizione → errore in validazione, che è l'unico punto dove c'è
          una persona a leggerlo. Un profilo muto si interpreta, uno che si
          contraddice no.
        """
        dichiarato = "multi_spawn" in self.model_fields_set
        if self.activation is None:
            self.activation = "parallel" if self.multi_spawn else "queue"
            return self
        if self.activation == "parallel":
            if dichiarato and not self.multi_spawn:
                raise ValueError(
                    f"agent {self.name}: `activation: parallel` e "
                    "`multi_spawn: false` si contraddicono — il parallelo È il "
                    "multi-spawn. Dichiarane uno solo.")
            self.multi_spawn = True
        elif self.multi_spawn:
            raise ValueError(
                f"agent {self.name}: `activation: {self.activation}` e "
                "`multi_spawn: true` si contraddicono — con il multi-spawn un "
                "secondo messaggio forka un'istanza, quindi non si accoda né "
                "viene rifiutato. Dichiarane uno solo.")
        return self

    # ── Multi-spawn (issue clodia-platform#94): N istanze concorrenti ──────
    # True = in un contesto (topic) il seed può materializzare più spawn
    # concorrenti, identificati da ordinale (#1, #2, …). La menzione generica
    # @nome va al minimo ordinale libero; se tutti occupati si forka una nuova
    # istanza fino a `max_spawns` (poi ci si accoda sul minimo). L'ordinale >1
    # riceve la memory del seed in SOLA LETTURA (niente scritture concorrenti).
    multi_spawn: bool = False
    # Cap istanze concorrenti per contesto (budget RAM: ogni istanza attiva è
    # un subprocess vivo). Rilevante solo con multi_spawn: true.
    max_spawns: int = 4

    # Sforzo di reasoning per i modelli che lo supportano (es. glm-5.2 su
    # Scaleway): "none" DISABILITA il reasoning → turni molto più rapidi, ~25×
    # meno token, niente loop runaway (per gli esecutori di tool). Valori tipici:
    # none | low | medium | high. Passato dal runtime opencode nelle options del
    # provider. None = default del modello (reasoning attivo su glm-5.2).
    reasoning_effort: Optional[str] = None

    # Timestamp di creazione (ISO 8601). Usato come tie-break di ANZIANITÀ nel
    # rango (a parità di tier, parla il più anziano: es. Clodia prima di Ophelia).
    created_at: Optional[str] = None

    sandbox: Sandbox = Field(default_factory=Sandbox)
    # DEPRECATO (AgentSpec v2): file skill custom locali alla cartella
    # agente. Usare `capabilities` + skills-catalog (data catalog per le
    # skill private dell'istanza). Il loader emette warning se presente.
    skills: list[str] = Field(default_factory=list)
    memory: Optional[MemoryConfig] = None

    # Ruolo. Per gli agent eseguiti: "reviewer" → QA (emette qa_verdict). Per i
    # principal `human` (Admin Auth): "superadmin" (il primo, reclama l'istanza)
    # o "admin". None = agente/principal standard.
    role: Optional[str] = None

    # Clearance di privacy del principal `human` (P0–P3): vede un topic sse
    # `T.privacy <= clearance`. None per gli agent eseguiti (non umani).
    clearance: Optional[str] = None

    # ── Canali di contatto ────────────────────────────────────────────
    # email/telegram espliciti (bot e umani). Se assenti vengono derivati:
    # clodia da convenzione, gli altri bot come subaddress dell'email parent
    # genitore (mailbox_parent, default "clodia"). Vedi api.contacts.
    email: Optional[str] = None
    telegram: Optional[str] = None          # handle o chat_id Telegram
    mailbox_parent: Optional[str] = None    # per i bot: parent di cui usare il subaddress

    # DEPRECATO (AgentSpec v2): meccanismo di delega v3 via sub-card alle
    # inbox. Nel modello skill-driven la delega è il movimento di card fra
    # lane. Il loader emette warning se presente.
    can_delegate_to: list[str] = Field(default_factory=list)

    # Capacità dichiarate dell'agente (usate dalla webui e per il routing
    # basato su skill-consumer). Elenco libero di stringhe.
    capabilities: list[str] = Field(default_factory=list)

    # Regole di stile/comportamento applicate all'agente (riferimenti a
    # catalog rules). Elenco libero di stringhe.
    rules: list[str] = Field(default_factory=list)

    # Prerequisiti verso plugin del catalogo (plugin = [skills]+[rules]+[mcp]).
    # Soft di default: l'agente boota anche senza il plugin (modalità
    # degradata + warning esposto dall'API packs); `hard: true` è dichiarativo
    # e riservato a future policy di enforcement. Accetta anche stringhe
    # semplici ("<plugin>") coerse a {name: <plugin>, hard: false}.
    requires_plugins: list["PluginRequirement"] = Field(default_factory=list)

    @field_validator("requires_plugins", mode="before")
    @classmethod
    def _coerce_requires_plugins(cls, v):
        if isinstance(v, list):
            return [{"name": item} if isinstance(item, str) else item for item in v]
        return v

    # Immutabilità a runtime: se True, l'agent NON è
    # modificabile da nessuna via applicativa (PATCH admin, PFP, tool agents.*).
    # Si cambia SOLO via codice/rebuild del seed. Protegge gli agent di sistema
    # critici dall'auto-escalation e da riscritture indebite.
    immutable: bool = False

    # ── Campi CAP (Colony Agent Platform, spec §3.1) ──────────────────
    # Versione della definizione agente (semver libero, default "0").
    version: str = "0"
    # Priorità di selezione: più basso = preferito a parità di altri
    # criteri (Agent Selection Engine, spec §12).
    priority: int = 100
    # Vincolo di routing canale. "state_writer_only" identifica agenti che non
    # devono essere scelti come responder conversazionali generici: sono
    # eleggibili dal routing automatico solo per richieste esplicite di stato
    # del topic (summary, minute, verbali, prossimi passi).
    routing_mode: Literal["normal", "state_writer_only"] = "normal"
    # Meccanica di attivazione quando arriva un messaggio per un agente che sta
    # già rispondendo (router-notebook R15, issue clodia-platform#191). Fratello
    # di `routing_mode`: quello dice SE può essere scelto, questo COSA succede
    # quando è già occupato.
    #   queue    — ci si accoda (FIFO del lock di ChatSession). È il default e il
    #              comportamento storico di ogni seed: il campo arriva senza
    #              cambiare nulla in produzione.
    #   parallel — si forka un'altra istanza (#2, #3…): è `multi_spawn`, che
    #              questo campo assorbe invece di duplicare (vedi il validator).
    #   refuse   — nessun secondo turno finché il primo non finisce, con una nota
    #              di sistema nel topic (`channels._start_turn`).
    # La coda è per (SCOPE, seed), non per seed: la ChatSession è indicizzata
    # `chan:<tier>:<name>:<seed>[#N]`, quindi due stanze non si accodano l'una
    # sull'altra. Non è una scelta di policy aperta — è ciò che il codice fa.
    # `None` = «non dichiarato»: il validator lo deriva e il campo non resta mai
    # None dopo la validazione.
    activation: Optional[Literal["queue", "parallel", "refuse"]] = None
    # Profilo di costo dichiarato: "economy" (haiku), "standard" (sonnet),
    # "premium" (opus). Usato dalla selection engine come tie-break.
    cost_profile: str = "standard"
    # Permessi tool MCP granulari (es. ["topic.*", "email.send"]).
    # Enforcement nel gateway MCP; qui dichiarativo per validator/selection.
    tool_permissions: list[str] = Field(default_factory=list)
    #: Sottrazioni dai permessi effettivi, inclusi quelli ereditati. `None`
    #: significa «non modificare la copia custodita dal gateway»; `[]` la azzera.
    denied_tools: Optional[list[str]] = None
    #: Strumenti NATIVI del runtime concessi a questo seed (`Read`, `Bash`,
    #: `WebSearch`, `Agent`, `Cron*`…). ALLOWLIST: `None` = «non mi pronuncio»,
    #: quindi niente restrizione; `[]` = «nessuno strumento nativo», che è una
    #: dichiarazione e va rispettata.
    #:
    #: Terza cosa che un seed dichiara, accanto ai verbi del gateway
    #: (`tool_permissions`) e alle skill (`capabilities`), così «cosa può fare
    #: questo agente» torna ad avere UNA risposta, in un file. Prima nessuno li
    #: dichiarava e li avevano tutti — compreso `WebSearch`, che nessuna policy
    #: di rete può arbitrare perché lo esegue il provider (vedi
    #: `sdk_runtime/native_tools.py` per la misura).
    native_tools: Optional[list[str]] = None
    #: Come il runtime tratta le richieste di permesso di QUESTO agent, con i
    #: nomi dell'SDK: `default` (chiede), `acceptEdits`, `plan`,
    #: `bypassPermissions` (non chiede niente). `None` = il seed non si
    #: pronuncia.
    #:
    #: Sta qui per la stessa ragione di `native_tools`: era una riga di
    #: `KIND_PERMISSION_MODE` in `sdk_runtime/session.py` — `bypassPermissions`
    #: scritto accanto al codice per `clodia` e `looper` — cioè una decisione su
    #: un agente tenuta in una tabella invece che nell'agente
    #: (clodia-platform#199, residuo di A9). Peggio: `extra="forbid"` rendeva
    #: questo nome un campo VIETATO, quindi un seed che ci provava non caricava
    #: affatto.
    #:
    #: **Non è una via di scalata**, ed è misurabile: il ripiego di
    #: `_resolve_permission_mode` per chi non si pronuncia è già
    #: `bypassPermissions`, il valore più largo dei quattro. Una dichiarazione
    #: qui può dunque solo STRINGERE, e un agente che riscrivesse il proprio
    #: seed non guadagnerebbe niente scrivendoci il valore che ha già per
    #: assenza. Il giorno in cui il ripiego diventasse restrittivo, questo campo
    #: diventerebbe un'estensione e andrebbe custodito come `gated_tools` (copia
    #: nella config del gateway, dove l'agente non scrive).
    permission_mode: Optional[
        Literal["default", "acceptEdits", "plan", "bypassPermissions"]] = None
    #: Verbi che, per QUESTO agent, richiedono un consenso umano a ogni uso.
    #:
    #: Dichiarati qui perché è il posto in cui si sa quali dei propri verbi sono
    #: pericolosi: chi scrive il seed di un agente GitHub sa che `push_files` va
    #: chiesto e `list_branches` no. Prima esistevano SOLO nella config del
    #: gateway, messi a mano: `fullstack-dev` (15 mutazioni GitHub), `sysadmin`
    #: (creare/cancellare agenti, restart, backup) e `messaggero` avevano i gate
    #: perché qualcuno aveva eseguito uno script una volta — e un'istanza nuova
    #: dai pack nasceva senza. Un controllo che non viaggia col seed non esiste
    #: per il primo clone.
    #:
    #: Questa lista è una DICHIARAZIONE, non l'autorità. L'enforcement legge la
    #: copia nella config del gateway, dove l'agente non scrive: questo file vive
    #: sulla datadir insieme al codice degli agenti, e un agente capace di
    #: riscriverlo cancellerebbe i propri gate — cioè si auto-escalerebbe al
    #: silenzio. La copia avviene alla registrazione (register_agent), come per
    #: le dichiarazioni di flusso dei pack.
    #: Topic che l'agente PORTA CON SÉ: raggiungibili da qualunque stanza, senza
    #: gate. È lo «scope proprio dell'agente» — l'archivio aziendale che
    #: `impiegato-tomato` consulta ovunque lavori senza copiarlo in ogni topic.
    #:
    #: È un'AUTORIZZAZIONE, non una comodità. Fino al 7 ago 2026 la membership
    #: di un seed valeva da ogni stanza: su marte clodia era participant di 135
    #: topic su 157, quindi un suo spawn poteva leggere gli altri 134 stando in
    #: uno qualunque e riversarli lì. Il compartimento c'era nel modello e non
    #: nel codice. Ora la membership non basta più, e ciò che si porta con sé va
    #: dichiarato qui: esplicito, numerabile e leggibile in un file, invece che
    #: implicito e largo 135.
    #:
    #: Forma: `["SEAL-2/tomato-azienda", …]`.
    carries: Optional[list[str]] = None
    #: `None` = il seed NON si pronuncia (il gateway tiene ciò che ha);
    #: `[]` = dichiarato vuoto (il gateway azzera). La distinzione esiste
    #: perché una RIMOZIONE va dichiarata: col default a lista vuota,
    #: «assente» e «vuoto» collassavano e togliere una voce da un seed non
    #: arrivava mai a destinazione — verificato il 7 ago 2026 su messaggero.
    gated_tools: Optional[list[str]] = None
    #: Verbi gated SOLO dentro un canale di topic. Stessa custodia di
    #: `gated_tools` e stessa ragione. Serve dove il verbo È il mestiere
    #: dell'agente — un postino che spedisce — e ciò che cambia dentro un canale
    #: non è la pericolosità del verbo ma CHI può chiederlo: i partecipanti non
    #: sono l'owner, e il contenuto che possono far uscire è tutta la stanza.
    #: Gatarlo sempre renderebbe il gate un riflesso; gatarlo mai lascerebbe a un
    #: membro un canale d'uscita. Solo un admin può approvare un gate.
    #: `None` = il seed NON si pronuncia (il gateway tiene ciò che ha);
    #: `[]` = dichiarato vuoto (il gateway azzera). La distinzione esiste
    #: perché una RIMOZIONE va dichiarata: col default a lista vuota,
    #: «assente» e «vuoto» collassavano e togliere una voce da un seed non
    #: arrivava mai a destinazione — verificato il 7 ago 2026 su messaggero.
    gated_in_channel: Optional[list[str]] = None
    #: Il MESTIERE dichiarato: i verbi che l'agente usa senza chiedere. Ciò che
    #: raggiunge e NON dichiara resta raggiungibile ma passa da un consenso —
    #: least authority per supervisione invece che per rimozione. Stessa custodia
    #: di `gated_tools`: dichiarato qui, enforced dal gateway.
    #: `None` = il seed NON si pronuncia (il gateway tiene ciò che ha);
    #: `[]` = dichiarato vuoto (il gateway azzera). La distinzione esiste
    #: perché una RIMOZIONE va dichiarata: col default a lista vuota,
    #: «assente» e «vuoto» collassavano e togliere una voce da un seed non
    #: arrivava mai a destinazione — verificato il 7 ago 2026 su messaggero.
    profile_tools: Optional[list[str]] = None
    # Grant dichiarativi sulle collection RAG della capacità di piattaforma
    # (es. ["eu-normativa"]). Enforcement nel gateway, come tool_permissions.
    # Usati dai seed dei pack (es. aitiero in clodia-packs).
    rag_read: list[str] = Field(default_factory=list)
    rag_write: list[str] = Field(default_factory=list)
    # Volume montabili dichiarati (id da CLODIA_DATA/volumes.yaml, spec §3.4).
    # Tradotti in regole sandbox alla creazione del workspace effimero.
    volumes: list[str] = Field(default_factory=list)
    # Nomi di credenziali dedicate attese in secrets/agents/<name>/
    # (spec §19). Risoluzione con fallback alle globali via colony.credentials.
    credentials: list[str] = Field(default_factory=list)

    # path relativo al system prompt. Obbligatorio per gli agent ESEGUITI;
    # None per i principal `human` (non eseguiti: nessun prompt).
    system_prompt: Optional[str] = None
    outputs: Optional[OutputsConfig] = None

    # Path assoluto alla cartella dell'agente (popolato dal loader, non
    # dichiarato nello YAML).
    agent_dir: Optional[str] = None
