"""Pydantic models per agent.yaml.

Lo schema riflette esattamente i 4 prototipi validati nel topic
`acme-blog-agents/files/agents-proto/` (29 mag 2026).
"""
from __future__ import annotations
from typing import Literal, Optional

from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator


class Sandbox(BaseModel):
    """Permessi applicati al workspace effimero via .claude/settings.json.

    Tutti i path sono relativi alla data root (`/clodia` nel container,
    `WORKSPACE_ROOT` localmente). Il placeholder `{scratch}` è risolto
    runtime al path dello scratch dell'istanza.
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
    # Modello d'inferenza. Obbligatorio per gli agent ESEGUITI (normal/super);
    # None per i principal `human` (non eseguiti: nessun motore).
    model: Optional[str] = None
    display_name: str
    avatar_color: str = "#888888"

    # Categoria KYA (modello identità, agent-identity-model-spec.md §1):
    # "super" (clodia/ophelia, poteri pieni + CA), "normal" (worker sandboxati),
    # "human" (principal umani, es. owner). Guida i default di poteri/clearance.
    type: Literal["super", "normal", "human"] = "normal"

    # Genealogia del seed (modello ereditario): progenitore/i da cui questo seed
    # discende. Rende tracciabile il "drift" delle costituzioni dal genoma di
    # Clodia Primal. Es. ["clodia-primal"]. Vuoto = capostipite.
    parents: list[str] = Field(default_factory=list)

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
    # email/telegram espliciti (super e umani). Se assenti vengono derivati:
    # i super da convenzione, i regular come subaddress dell'email del super
    # genitore (mailbox_parent, default "clodia"). Vedi api.contacts.
    email: Optional[str] = None
    telegram: Optional[str] = None          # handle o chat_id Telegram
    mailbox_parent: Optional[str] = None    # per i regular: super di cui usare il subaddress

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

    # Immutabilità a runtime: se True (o se type=="super"), l'agent NON è
    # modificabile da nessuna via applicativa (PATCH admin, PFP, tool agents.*).
    # Si cambia SOLO via codice/rebuild del seed. Protegge il nucleo (super) e
    # gli agent "di sistema" critici (es. Janitor) dall'auto-escalation e da
    # riscritture indebite. Vedi api.agent_registry._is_immutable.
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
    # Profilo di costo dichiarato: "economy" (haiku), "standard" (sonnet),
    # "premium" (opus). Usato dalla selection engine come tie-break.
    cost_profile: str = "standard"
    # Permessi tool MCP granulari (es. ["trello.*", "email.send"]).
    # Enforcement nel gateway MCP; qui dichiarativo per validator/selection.
    tool_permissions: list[str] = Field(default_factory=list)
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
    gated_tools: list[str] = Field(default_factory=list)
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
