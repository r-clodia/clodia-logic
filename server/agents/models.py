"""Pydantic models per agent.yaml.

Lo schema riflette esattamente i 4 prototipi validati nel topic
`acme-blog-agents/files/agents-proto/` (29 mag 2026).
"""
from __future__ import annotations
from typing import Literal, Optional

from pydantic import BaseModel, Field, ConfigDict


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


class AgentSpec(BaseModel):
    """Specifica completa di un agente caricata da agent.yaml.

    Mantiene path al system_prompt e alle skill come stringhe relative —
    il loader li risolve al filesystem dopo il parse, per consentire al
    workspace effimero di copiare i file senza ulteriori indirezioni.
    """
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
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

    # Immutabilità a runtime: se True (o se type=="super"), l'agent NON è
    # modificabile da nessuna via applicativa (PATCH admin, PFP, tool agents.*).
    # Si cambia SOLO via codice/rebuild del seed. Protegge il nucleo (super) e
    # gli agent "di sistema" critici (es. Wainston) dall'auto-escalation e da
    # riscritture indebite. Vedi api.agent_registry._is_immutable.
    immutable: bool = False

    # ── Campi CAP (Colony Agent Platform, spec §3.1) ──────────────────
    # Versione della definizione agente (semver libero, default "0").
    version: str = "0"
    # Priorità di selezione: più basso = preferito a parità di altri
    # criteri (Agent Selection Engine, spec §12).
    priority: int = 100
    # Profilo di costo dichiarato: "economy" (haiku), "standard" (sonnet),
    # "premium" (opus). Usato dalla selection engine come tie-break.
    cost_profile: str = "standard"
    # Permessi tool MCP granulari (es. ["trello.*", "email.send"]).
    # Enforcement nel gateway MCP; qui dichiarativo per validator/selection.
    tool_permissions: list[str] = Field(default_factory=list)
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
