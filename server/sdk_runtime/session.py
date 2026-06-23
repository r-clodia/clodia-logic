"""Wrapper around claude-agent-sdk per multi-chat Clodia.

Ogni chat è una ChatSession indipendente: subprocess `claude` proprio, history
in file JSONL dedicato. Tutte le chat condividono la stessa identità Clodia
(cwd = workspace radice, super-prompt + MEMORY auto-caricati dal binary).
Il manager `chats` tiene il dict {chat_id → ChatSession}.
"""
import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, ResultMessage
from claude_agent_sdk.types import (
    AssistantMessage, UserMessage,
    ToolUseBlock, ToolResultBlock, ThinkingBlock,
    TaskProgressMessage, StreamEvent,
    PermissionResultAllow, ToolPermissionContext,
)

from ..config import WORKSPACE_ROOT as _BUNDLE_ROOT, data_path
from ..agents import activity_log
from ..colony import pki


def _snippet(text: str, n: int = 160) -> str:
    s = " ".join((text or "").split())
    return s[:n] + ("…" if len(s) > n else "")
from ..core.events import bus
from ..core.models import Event, ClodiaStatus
from ..observability import langfuse_attributes, langfuse_observation, trace_io

LOG = logging.getLogger("agent-server.sdk_runtime.session")

# Gateway clodia-tools come MCP HTTP (microservizio). URL configurabile (il
# microservizio è spostabile); default = servizio sulla rete docker compose.
CLODIA_TOOLS_MCP_URL = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/")
# TTL lungo: una chat webchat è interattiva e può durare ore. Token in-memory
# (mai su disco), passato in localhost/rete docker al microservizio.
_CLODIA_TOOLS_TOKEN_TTL = 24 * 3600
# Limite del buffer di stream per riga JSON del subprocess (Claude SDK e Codex).
# Il default asyncio (64KB) e quello dell'SDK (1MB) vanno in overflow quando un
# tool restituisce contenuti grandi (es. topic.read_file di un file) → la riga
# JSON supera il limite ("Separator is found, but chunk is longer than limit").
_STREAM_LIMIT = 32 * 1024 * 1024  # 32MB

COLLECT_CHUNK_TIMEOUT = 5 * 60    # 5 min silenzio SDK → stallo reale
COLLECT_MAX_SECONDS   = 4 * 60 * 60  # 4h hard cap assoluto
DEFAULT_CHAT_ID = "default"

# Tipi di agente supportati. Ogni kind ha un cwd dedicato (dove vive il
# CLAUDE.md e i settings di quell'agente) e una cartella sessions/
# dedicata. La storia di Clodia resta sotto agent-server/sessions/,
# quella di Ada è in ~/ada/sessions/.
# NB: _BUNDLE_ROOT è derivato da WORKSPACE_ROOT in config.py (path relativo
# al file) — risolve a /Users/erreclaudea/erre-claudia in locale e a /clodia
# in Docker, senza hardcode.
# Per ada e looper: fallback a _BUNDLE_ROOT se il path Mac non esiste
# (caso Docker, dove /Users/erreclaudea/... non è montato).
def _cwd(preferred: Path) -> Path:
    return preferred if preferred.exists() else _BUNDLE_ROOT

# ophelia = clodia-su-codex: runtime codex (vedi CodexChatSession), cwd e
# sessions persistenti sotto datadir. CODEX_KINDS elenca i kind serviti dal
# runtime codex anziché dal Claude SDK.
CODEX_KINDS = {"ophelia"}
_OPHELIA_CWD = data_path("data/ophelia-workspace")
_OPHELIA_SESSIONS = data_path("data/ophelia-sessions")

KIND_CWD = {
    "clodia": _BUNDLE_ROOT,
    "ada":    _cwd(Path("/Users/erreclaudea/ada")),
    "looper": _cwd(Path("/Users/erreclaudea/looper")),
    "ophelia": _OPHELIA_CWD,
}
KIND_SESSIONS_DIR = {
    "clodia": _BUNDLE_ROOT / "sessions",
    "ada":    Path("/Users/erreclaudea/ada/sessions") if Path("/Users/erreclaudea/ada").exists()
              else _BUNDLE_ROOT / "sessions",
    "looper": Path("/Users/erreclaudea/looper/sessions") if Path("/Users/erreclaudea/looper").exists()
              else _BUNDLE_ROOT / "sessions",
    "ophelia": _OPHELIA_SESSIONS,
}
KIND_TITLE_PREFIX = {
    "clodia": "[CLO]",
    "ada":    "[ADA]",
    "looper": "[LOOP]",
    "ophelia": "[OPH]",
}
# Modello richiesto per kind. None = usa default del CLI/config server.
# Ada richiede sempre Opus 4.7 (o superiore): è una system developer e i
# task tecnici complessi non sono delegabili a Sonnet/Haiku. Quando uscirà
# un Opus successivo, aggiornare qui.
# Looper è esecutore meccanico di routine cicliche: Haiku 4.5 è abbondante,
# il prompt è snello e le decisioni sono prefissate.
KIND_MODEL = {
    "clodia": None,
    "ada":    "claude-opus-4-7",
    "looper": "claude-haiku-4-5",
}
# Override esplicito del permission_mode SDK. Necessario per agent che girano
# completamente autonomi senza un human in loop che approvi prompt (es. looper
# che esegue iterazioni schedulate via ScheduleWakeup). settings.json del
# workspace non viene applicato in modalità SDK headless — questo parametro sì.
# 'bypassPermissions' = nessun gate sul permesso, ma KIND_DISALLOWED_TOOLS
# blocca comunque le azioni esterne irreversibili via --disallowedTools.
KIND_PERMISSION_MODE = {
    "clodia": "bypassPermissions",
    "ada":    None,
    "looper": "bypassPermissions",
}
# Strumenti vietati per kind, indipendente da permission_mode.
# Passato come --disallowedTools al subprocess: blocca azioni esterne
# irreversibili anche sotto bypassPermissions.
# Write/Edit/Bash(git:*)/Bash(python3:*) restano permessi.
# F1.5 — cutover di Clodia su MCP (14 giu 2026): la webchat Clodia non invoca
# più i CLI dei tool via Bash; deve passare dal gateway MCP segregato
# (clodia-tools). I tool già wrappati (trello/email.send/fs/agent) restano
# disponibili via MCP; quelli non ancora migrati sono TEMPORANEAMENTE
# indisponibili (buco transitorio accettato, fino al wrapping F2). Difesa in
# profondità sopra F1 (rimozione dei secret dei tool dal runtime): anche se un
# secret fosse leggibile, il CLI è comunque vietato.
KIND_DISALLOWED_TOOLS: dict[str, list[str]] = {
    "clodia": [
        "Bash(rm:*)",
        # tool CLI → vietati: usare i corrispettivi MCP quando disponibili
        "Bash(*email_client*)",
        "Bash(*trello_client*)",
        "Bash(*gdrive_client*)",
        "Bash(*gdocs_client*)",
        "Bash(*gslides_client*)",
        "Bash(*gcalendar*)",
        "Bash(*web_render*)",
        "Bash(*image_caption*)",
        "Bash(*openai_images*)",
        "Bash(*slide_renderer*)",
        "Bash(*search_client*)",
        "Bash(*cc_index*)",
        "Bash(*md_to_pdf*)",
        "Bash(*markdown_pdf*)",
        "Bash(*linkedin*)",
        "Bash(*aruba_fattura*)",
        "Bash(*firma_client*)",
        "Bash(*sedia_client*)",
        "Bash(*aws_invoicing*)",
        "Bash(*whatsapp*)",
    ],
    "ada":    [],
    "looper": [],
}
# Auto-intro: messaggio user injettato automaticamente al primo turno
# della chat, subito dopo lo start. Usato per kind che hanno una UX di
# presentazione (es. looper mostra la lista delle sue recipe). Se None,
# nessun auto-intro: la chat resta IDLE in attesa del primo messaggio
# dell'operatore.
KIND_AUTO_INTRO = {
    "clodia": None,
    "ada":    None,
    "looper": "presentati, mostra le tue recipe e mettiti al mio servizio",
}
DEFAULT_KIND = "clodia"

# Callback usato come sostituto di bypassPermissions quando il processo
# gira come root (Docker): il CLI rifiuta --dangerously-skip-permissions
# con uid=0. Con can_use_tool il gate di permesso è gestito lato SDK
# senza passare il flag incriminato al subprocess.
async def _allow_all(
    tool_name: str,
    tool_input: dict,
    ctx: ToolPermissionContext,
) -> PermissionResultAllow:
    return PermissionResultAllow()

_IS_ROOT = (os.getuid() == 0)

# Backcompat alias: alcuni call site (e codice esterno) usano ancora
# WORKSPACE_ROOT / SESSIONS_DIR. Restano agganciati a Clodia.
WORKSPACE_ROOT = KIND_CWD["clodia"]
SESSIONS_DIR = KIND_SESSIONS_DIR["clodia"]


def _seed_governance_text(kind: str) -> Optional[str]:
    """Governance dal SEED dell'agent `kind`: costituzione (constitution-catalog)
    + identità (system-prompt.md del seed), fuse. Usata come `system_prompt` per
    le webchat claude e come contenuto di AGENTS.md per quelle codex, così
    l'agent è governato dal proprio seed. None se il seed non è nel registry
    (→ comportamento legacy). Trello-free."""
    try:
        from ..agents.loader import registry
        from ..agents.constitution_sync import load_constitution_text
        spec = registry.get_by_name(kind)
        if spec is None:
            return None
        parts: list[str] = []
        const = load_constitution_text(getattr(spec, "constitution", None))
        if const:
            parts.append(const.strip())
        if spec.agent_dir:
            sp = Path(spec.agent_dir) / spec.system_prompt
            if sp.is_file():
                parts.append(sp.read_text(encoding="utf-8").strip())
        return "\n\n---\n\n".join(parts) + "\n" if parts else None
    except Exception as e:  # noqa: BLE001 — il seed non deve impedire lo start
        LOG.warning("governance dal seed non risolta per %s: %s", kind, e)
        return None


def _materialize_spawn(kind: str):
    """Materializza uno SPAWN dal seed dell'agent `kind` (modello /spawns): copia
    seed + costituzione fusa + skill (catalog + apprese) + memory (symlink) +
    scratch in clodia-data/spawns/<name>-<n>. Ritorna (EphemeralWorkspace, Path)
    o (None, None) se il seed non è nel registry (→ fallback legacy)."""
    try:
        from ..agents.loader import registry
        from ..agents.workspace import EphemeralWorkspace
        spec = registry.get_by_name(kind)
        if spec is None or not spec.agent_dir:
            return None, None
        ws = EphemeralWorkspace(spec)
        return ws, ws.create()
    except Exception as e:  # noqa: BLE001 — lo spawn non deve impedire lo start
        LOG.warning("spawn non materializzato per %s: %s", kind, e)
        return None, None


# ── Risoluzione dinamica dei kind (job-agent dinamico, 19 giu 2026) ──────────
# Oltre ai kind statici (clodia/ada/looper/ophelia, sopra) un kind può essere
# QUALUNQUE agent del registry (clodia-data/agents/<name>/agent.yaml). I helper
# sotto risolvono cwd/sessions/model/permission/runtime/titolo consultando prima
# i dict statici (back-compat, zero regressioni) e poi il seed dell'agent. Così
# un job può girare con un agent arbitrario, ereditandone skill/tools/mcp dallo
# spawn materializzato dal seed (_materialize_spawn).

def _kind_spec(kind: str):
    """AgentSpec dell'agent `kind` dal registry, o None (registry assente / kind ignoto)."""
    try:
        from ..agents.loader import registry
        return registry.get_by_name(kind)
    except Exception:  # noqa: BLE001
        return None


def known_kind(kind: str) -> bool:
    """True se `kind` è uno statico (KIND_CWD) o un agent del registry.
    Usato dalle guardie di validazione (session + api/agents)."""
    return kind in KIND_CWD or _kind_spec(kind) is not None


def available_kinds() -> list[str]:
    """Elenco ordinato dei kind spawnabili: statici + agent del registry."""
    names = set(KIND_CWD)
    try:
        from ..agents.loader import registry
        names.update(a.name for a in registry.list())
    except Exception:  # noqa: BLE001
        pass
    return sorted(names)


def _is_codex_kind(kind: str) -> bool:
    """True se il kind gira sul runtime codex: statico (CODEX_KINDS) o agent_sdk=codex."""
    if kind in CODEX_KINDS:
        return True
    spec = _kind_spec(kind)
    return bool(spec and getattr(spec, "agent_sdk", "claude") == "codex")


def _resolve_cwd(kind: str) -> Path:
    if kind in KIND_CWD:
        return KIND_CWD[kind]
    spec = _kind_spec(kind)
    if spec and spec.agent_dir:
        return _cwd(Path(spec.agent_dir))
    return _BUNDLE_ROOT


def _resolve_sessions_dir(kind: str) -> Path:
    if kind in KIND_SESSIONS_DIR:
        return KIND_SESSIONS_DIR[kind]
    # Dinamico: una cartella sessions per-kind sotto la datadir (persistente
    # cross-rebuild, clonabile). Mirror del layout di ophelia.
    return data_path(f"data/{kind}-sessions")


def _resolve_title_prefix(kind: str) -> str:
    if kind in KIND_TITLE_PREFIX:
        return KIND_TITLE_PREFIX[kind]
    return f"[{kind[:4].upper()}]"


def _resolve_model(kind: str) -> Optional[str]:
    if kind in KIND_MODEL:
        return KIND_MODEL[kind]
    spec = _kind_spec(kind)
    return spec.model if spec else None


def _resolve_permission_mode(kind: str) -> Optional[str]:
    if kind in KIND_PERMISSION_MODE:
        return KIND_PERMISSION_MODE[kind]
    # Dinamico: l'agent gira autonomo (es. fire di un job, nessun human-in-loop)
    # → bypassPermissions. Il guardrail NON è una blocklist sintetica ma i grant
    # del gateway MCP per-agent (decisione sicurezza owner, 18 giu 2026).
    return "bypassPermissions"


def _resolve_disallowed_tools(kind: str) -> list[str]:
    if kind in KIND_DISALLOWED_TOOLS:
        return KIND_DISALLOWED_TOOLS[kind]
    # Dinamico: nessuna blocklist artificiale (vedi _resolve_permission_mode).
    return []


# ── Enforcement disponibilità per provider (20 giu 2026) ─────────────────────
# Un agent il cui provider non è collegato NON è disponibile: non si può aprire
# una chat né far girare un job. Lo stato 'disconnected' della webui non basta —
# qui sta la guardia autoritativa, sul choke point unico (ChatManager.create),
# attraversato sia dalla webchat sia dal fire dei job.

class ProviderNotConnected(RuntimeError):
    """L'agent non è disponibile perché il suo provider non è collegato."""

    def __init__(self, kind: str, provider: str) -> None:
        self.kind = kind
        self.provider = provider
        super().__init__(
            f"agent '{kind}': nessun provider compatibile collegato "
            f"({provider}) — collega uno di questi dalla sezione Providers "
            f"prima di usarlo")


def agent_candidates(kind: str) -> list[str]:
    """Provider compatibili del kind, ordinati per preferenza: lista esplicita
    dal seed, o default dell'SDK. Kind statici senza seed (ada/looper) → SDK."""
    try:
        from ..api.providers import candidate_providers, default_providers_for_sdk
        spec = _kind_spec(kind)
        if spec is not None:
            return candidate_providers(getattr(spec, "providers", None),
                                       getattr(spec, "provider", None), spec.agent_sdk)
        sdk = "codex" if kind in CODEX_KINDS else "claude"
        return default_providers_for_sdk(sdk)
    except Exception:  # noqa: BLE001
        return []


def agent_effective_provider(kind: str) -> Optional[str]:
    """Provider EFFETTIVO del kind: primo compatibile collegato. None se nessun
    candidato è collegato. Su errore infra fail-open al candidato preferito."""
    cands = agent_candidates(kind)
    if not cands:
        return None
    try:
        from ..api.providers import connected_provider_ids
        connected = connected_provider_ids()
    except Exception:  # noqa: BLE001 — fail-open su errore infra
        return cands[0]
    return next((c for c in cands if c in connected), None)


def agent_provider(kind: str) -> Optional[str]:
    """Compat: provider effettivo, o (se nessuno collegato) il preferito."""
    return agent_effective_provider(kind) or (agent_candidates(kind) or [None])[0]


def provider_connected_for(kind: str) -> bool:
    """True se almeno un provider compatibile del kind è collegato (o nessun
    candidato determinabile → passa). Fail-open su errore infra."""
    cands = agent_candidates(kind)
    if not cands:
        return True
    try:
        from ..api.providers import connected_provider_ids
        connected = connected_provider_ids()
    except Exception:  # noqa: BLE001
        return True
    return any(c in connected for c in cands)


def _ensure_provider_connected(kind: str) -> None:
    """Solleva ProviderNotConnected se NESSUN provider compatibile è collegato."""
    cands = agent_candidates(kind)
    if not cands:
        return
    try:
        from ..api.providers import connected_provider_ids
        connected = connected_provider_ids()
    except Exception:  # noqa: BLE001 — fail-open su errore infra
        return
    if not any(c in connected for c in cands):
        raise ProviderNotConnected(kind, ", ".join(cands))


class ChatSession:
    """Una singola chat con un agente (Clodia o Ada): subprocess claude
    + history dedicata sotto la cartella sessions/ del kind."""

    def __init__(self, chat_id: str, kind: str = DEFAULT_KIND, title: str = "") -> None:
        if not known_kind(kind):
            raise ValueError(f"unknown agent kind: {kind}")
        self.chat_id = chat_id
        self.kind = kind
        self.title = title or f"{_resolve_title_prefix(kind)} Nuova chat"
        self.status = ClodiaStatus.STOPPED
        self.created_at = datetime.now(timezone.utc)
        self.last_activity = self.created_at
        self._client: Optional[ClaudeSDKClient] = None
        self._client_ctx = None
        self._lock = asyncio.Lock()
        self._current_turn_task: Optional[asyncio.Task] = None
        self._last_usage: dict[str, int] = {}
        self._total_tokens: dict[str, int] = {"input": 0, "output": 0, "runs": 0}
        self._spawn = None  # EphemeralWorkspace dello spawn webchat (cleanup a stop)
        # Utente UMANO della chat (principal verificato dal token della webui).
        # Propagato al gateway nel token ckt1 → runtime.current_user.
        self.principal: Optional[str] = None
        # principal "cotto" nel token MCP del client attualmente avviato (per
        # capire quando ri-coniare se l'utente connesso cambia).
        self._token_principal: Optional[str] = None

    @property
    def cwd(self) -> Path:
        return _resolve_cwd(self.kind)

    @property
    def sessions_dir(self) -> Path:
        return _resolve_sessions_dir(self.kind)

    @property
    def session_file(self) -> Path:
        return self.sessions_dir / f"chat-{self.chat_id}.jsonl"

    async def start(self) -> None:
        # Propaga chat_id e kind come env var al subprocess claude: serve a
        # isolare workspace/scratch per chat parallele (es. Ada usa
        # $AGENT_CHAT_ID come namespace di scratch/).
        child_env = {
            **os.environ,
            "AGENT_CHAT_ID": self.chat_id,
            "AGENT_KIND": self.kind,
        }
        # ANTHROPIC_API_KEY vuota nel container Docker impedisce il login OAuth:
        # il CLI la vede e tenta la modalità API key ignorando ~/.claude/.
        # La rimuoviamo se vuota così il subprocess usa il token OAuth salvato.
        if not child_env.get("ANTHROPIC_API_KEY"):
            child_env.pop("ANTHROPIC_API_KEY", None)
        # Credenziali dei PROVIDER configurate da /api/providers (opzione B):
        # Anthropic Max → CLAUDE_CODE_OAUTH_TOKEN, oppure ANTHROPIC_API_KEY.
        # Applicate qui all'env del subprocess; il valore non transita dal modello.
        try:
            from ..api.providers import provider_env
            # Inietta SOLO l'env del provider effettivo dell'agent (primo
            # compatibile collegato): con provider distinti per DPA/costo, due
            # credenziali dello stesso SDK collegate insieme non devono
            # sovrapporsi. Kind non determinabile → fallback a tutti (back-compat).
            eff = agent_effective_provider(self.kind)
            if eff:
                child_env.update(provider_env(eff))
            elif not agent_candidates(self.kind):
                # kind non determinabile → back-compat: tutti i provider collegati
                child_env.update(provider_env())
            # candidati presenti ma nessuno collegato → niente env (l'agent non parte)
        except Exception as e:  # pragma: no cover
            LOG.warning("provider_env non applicato: %s", e)
        # include_partial_messages: l'SDK emette StreamEvent con i delta
        # token-by-token (text_delta / thinking_delta) durante la risposta.
        # Senza, ogni turno arriva come AssistantMessage completo → la chat
        # "scatta" tutta insieme (issue #14). I delta li ritrasmettiamo sul
        # bus come message_chunk{delta} / thinking_chunk{delta}.
        # webchat = SPAWN (modello /spawns): materializza il seed completo
        # (costituzione + skill catalog/apprese + memory symlink + scratch) e usa
        # quel workspace come cwd. Mirror del runtime colonia: system_prompt dal
        # file, .claude/ (skill+settings) caricato di default. Niente CLAUDE.md nel
        # workspace → nessun conflitto. Lo spawn è pulito a stop().
        self._spawn, spawn_dir = _materialize_spawn(self.kind)
        cwd = str(spawn_dir) if spawn_dir else str(self.cwd)
        opts_kwargs = {"cwd": cwd, "env": child_env, "include_partial_messages": True,
                       "max_buffer_size": _STREAM_LIMIT}
        if spawn_dir is not None:
            sp = spawn_dir / "system-prompt.md"
            if sp.is_file():
                opts_kwargs["system_prompt"] = sp.read_text(encoding="utf-8")
        else:
            # Fallback (kind senza seed, es. ada/looper): governance dal seed se
            # disponibile + niente auto-load della CLAUDE.md del bundle.
            seed_prompt = _seed_governance_text(self.kind)
            if seed_prompt:
                opts_kwargs["system_prompt"] = seed_prompt
                opts_kwargs["setting_sources"] = []
        model_override = _resolve_model(self.kind)
        if model_override:
            opts_kwargs["model"] = model_override
        permission_mode_override = _resolve_permission_mode(self.kind)
        if permission_mode_override == "bypassPermissions" and _IS_ROOT:
            # Root: --dangerously-skip-permissions rifiutato dal CLI.
            # can_use_tool approva tutto lato SDK senza il flag incriminato.
            opts_kwargs["can_use_tool"] = _allow_all
        elif permission_mode_override:
            opts_kwargs["permission_mode"] = permission_mode_override
        disallowed = _resolve_disallowed_tools(self.kind)
        if disallowed:
            opts_kwargs["disallowed_tools"] = disallowed
        # clodia-tools via MCP HTTP (microservizio segregato): conio un token
        # PKI per l'identità=kind e lo inietto nelle opzioni (in-memory, mai su
        # disco). Solo per i kind con identità PKI (clodia); per gli altri il
        # mint fallisce → si salta senza rompere.
        try:
            ct_token = pki.mint_session_token(self.kind, ttl_seconds=_CLODIA_TOOLS_TOKEN_TTL,
                                              principal=self.principal)
            # principal "cotto" nel token MCP di questo client: se cambia (l'utente
            # connesso cambia, o la sessione era partita anonima) va ri-coniato.
            self._token_principal = self.principal
            opts_kwargs["mcp_servers"] = {
                "clodia-tools": {
                    "type": "http",
                    "url": CLODIA_TOOLS_MCP_URL,
                    "headers": {"Authorization": f"Bearer {ct_token}"},
                }
            }
        except Exception as e:
            LOG.warning("clodia-tools MCP HTTP non configurato per kind=%s: %s", self.kind, e)
        options = ClaudeAgentOptions(**opts_kwargs)
        self._client_ctx = ClaudeSDKClient(options=options)
        self._client = await self._client_ctx.__aenter__()
        await self._set_status(ClodiaStatus.IDLE)
        # Auto-intro fire-and-forget: se il kind ne ha uno definito, lo
        # consegnamo come primo messaggio user in background. Il caller di
        # start() ritorna subito; eventuali messaggi successivi dell'operatore
        # vengono serializzati dal lock interno della sessione.
        intro = KIND_AUTO_INTRO.get(self.kind)
        if intro:
            asyncio.create_task(self._do_send_bg(intro))

    async def stop(self) -> None:
        if self._client_ctx is not None:
            try:
                await self._client_ctx.__aexit__(None, None, None)
            except Exception:
                pass
        self._client = None
        self._client_ctx = None
        # Nice termination dello spawn: la memory (symlink) è preservata, scratch
        # e copia effimera distrutti.
        if self._spawn is not None:
            try:
                self._spawn.cleanup()
            except Exception:  # noqa: BLE001
                pass
            self._spawn = None
        await self._set_status(ClodiaStatus.STOPPED)

    async def send_user_message(self, content: str) -> str:
        if self._client is None:
            raise RuntimeError("session not started")
        async with self._lock:
            await self._record({"role": "user", "content": content})
            await self._set_status(ClodiaStatus.THINKING)
            activity_log.append(self.kind, "run_started",
                                {"prompt": _snippet(content), "principal": self.principal,
                                 "chat_id": self.chat_id})
            self._last_usage = {}
            model_name = KIND_MODEL.get(self.kind) or "claude-cli-default"
            with langfuse_observation(
                name="clodia-chat-turn",
                as_type="generation",
                model=model_name,
                input=trace_io(content),
                metadata={"chat_id": self.chat_id, "kind": self.kind, "title": self.title},
            ) as generation:
                with langfuse_attributes(
                    session_id=self.chat_id,
                    user_id="owner",
                    trace_name="clodia-chat-turn",
                    tags=["agent-server", "chat", self.kind],
                    metadata={"kind": self.kind},
                ):
                    try:
                        await self._client.query(content)
                    except Exception as e:
                        await self._set_status(ClodiaStatus.ERROR)
                        activity_log.append(self.kind, "error",
                                            {"error": _snippet(str(e)), "chat_id": self.chat_id})
                        await self._publish_error(str(e))
                        raise
                    self._current_turn_task = asyncio.create_task(self._collect_response())
                    try:
                        full = await self._current_turn_task
                        update_kwargs = {
                            "output": trace_io(full),
                            "metadata": {"status": "ok", "chat_id": self.chat_id, "kind": self.kind},
                        }
                        if self._last_usage:
                            update_kwargs["usage_details"] = self._last_usage
                        generation.update(**update_kwargs)
                        await self._record({"role": "assistant", "content": full})
                        activity_log.append(self.kind, "run_done",
                                            {"reply": _snippet(full), "chat_id": self.chat_id,
                                             "usage": self._last_usage or None})
                        await self._set_status(ClodiaStatus.IDLE)
                        return full
                    except asyncio.CancelledError:
                        note = "⏹ Inferenza interrotta dall'utente."
                        generation.update(output=trace_io(note), metadata={"status": "interrupted"})
                        await self._set_status(ClodiaStatus.CANCELLING)
                        await self._record({"role": "system", "content": note})
                        await bus.publish(Event(
                            type="interrupted",
                            payload={"chat_id": self.chat_id, "reason": "user_interrupt"},
                            timestamp=datetime.now(timezone.utc),
                        ))
                        await self._set_status(ClodiaStatus.IDLE)
                        return note
                    except asyncio.TimeoutError:
                        note = (f"⏱ Timeout: nessun evento SDK per {COLLECT_CHUNK_TIMEOUT // 60}min "
                                f"(o superato il cap di {COLLECT_MAX_SECONDS // 3600}h).")
                        generation.update(output=trace_io(note), metadata={"status": "timeout"})
                        await self._set_status(ClodiaStatus.ERROR)
                        await self._record({"role": "system", "content": note})
                        await self._publish_error(note, reason="collect_timeout")
                        raise
                    except Exception as e:
                        generation.update(output=trace_io(str(e)), metadata={"status": "error"})
                        await self._set_status(ClodiaStatus.ERROR)
                        await self._publish_error(str(e))
                        raise
                    finally:
                        self._current_turn_task = None

    async def send_user_message_async(self, content: str) -> dict:
        """Fire-and-forget: enqueue il messaggio e ritorna subito. Il turno
        viene processato in background (acquisisce il lock all'avvio del task).
        Usato dal looper per dispacciare task ad altri agent senza bloccarsi
        in attesa della loro risposta.
        """
        if self._client is None:
            raise RuntimeError("session not started")
        asyncio.create_task(self._do_send_bg(content))
        return {"chat_id": self.chat_id, "queued": True}

    async def _do_send_bg(self, content: str) -> None:
        try:
            await self.send_user_message(content)
        except Exception:
            # send_user_message già publishes errori sull'event bus e li logga
            # nella history. In fire-and-forget non c'è caller a cui propagare.
            pass

    async def interrupt_current_turn(self) -> bool:
        task = self._current_turn_task
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def _collect_response(self) -> str:
        parts: list[str] = []
        start = asyncio.get_event_loop().time()
        iterator = self._client.receive_response().__aiter__()
        while True:
            elapsed = asyncio.get_event_loop().time() - start
            if elapsed >= COLLECT_MAX_SECONDS:
                raise asyncio.TimeoutError()
            chunk_timeout = min(COLLECT_CHUNK_TIMEOUT, COLLECT_MAX_SECONDS - elapsed)
            try:
                async with asyncio.timeout(chunk_timeout):
                    message = await iterator.__anext__()
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                raise

            if isinstance(message, StreamEvent):
                # Delta token-by-token (include_partial_messages=True). L'evento
                # raw dell'API è in message.event: content_block_delta porta
                # delta.type = text_delta | thinking_delta. Li ritrasmettiamo
                # come *append* (campo `delta`) così il FE costruisce la bolla
                # progressivamente senza clobber multi-blocco.
                ev = message.event or {}
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta" and delta.get("text"):
                        await bus.publish(Event(
                            type="message_chunk",
                            payload={"chat_id": self.chat_id, "role": "assistant",
                                     "delta": delta["text"]},
                            timestamp=datetime.now(timezone.utc),
                        ))
                    elif dtype == "thinking_delta" and delta.get("thinking"):
                        await bus.publish(Event(
                            type="thinking_chunk",
                            payload={"chat_id": self.chat_id, "delta": delta["thinking"]},
                            timestamp=datetime.now(timezone.utc),
                        ))
                continue

            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        await bus.publish(Event(
                            type="tool_use",
                            payload={
                                "chat_id": self.chat_id,
                                "tool": block.name,
                                "input_summary": _summarize_input(block.input),
                            },
                            timestamp=datetime.now(timezone.utc),
                        ))
                # Il testo (e il thinking) sono già stati streammati come delta
                # dagli StreamEvent: accumuliamo solo `parts` per persistenza/
                # return, senza ri-pubblicare message_chunk full-text (eviterebbe
                # il REPLACE che cancella i blocchi precedenti).
                text = _extract_text(message)
                if text:
                    parts.append(text)
                continue
            elif isinstance(message, UserMessage):
                content_list = message.content if isinstance(message.content, list) else []
                for block in content_list:
                    if isinstance(block, ToolResultBlock):
                        preview = _content_preview(block.content, 160)
                        if preview:
                            await bus.publish(Event(
                                type="tool_result",
                                payload={
                                    "chat_id": self.chat_id,
                                    "tool_use_id": block.tool_use_id,
                                    "is_error": block.is_error or False,
                                    "preview": preview,
                                },
                                timestamp=datetime.now(timezone.utc),
                            ))
            elif isinstance(message, TaskProgressMessage):
                await bus.publish(Event(
                    type="task_progress",
                    payload={
                        "chat_id": self.chat_id,
                        "description": message.description,
                        "last_tool_name": message.last_tool_name,
                        "usage": dict(message.usage) if message.usage else {},
                    },
                    timestamp=datetime.now(timezone.utc),
                ))

            # Difensivo: qualunque altro tipo di messaggio con testo (non
            # AssistantMessage, già gestito sopra) contribuisce a `parts`. Lo
            # streaming visibile è prodotto dai delta degli StreamEvent, quindi
            # qui NON ripubblichiamo message_chunk (eviterebbe il REPLACE).
            text = _extract_text(message)
            if text:
                parts.append(text)
            if isinstance(message, ResultMessage) and message.usage:
                u = message.usage
                self._last_usage = {
                    "input_tokens": int(u.get("input_tokens", 0) or 0),
                    "output_tokens": int(u.get("output_tokens", 0) or 0),
                    "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens", 0) or 0),
                    "cache_read_input_tokens": int(u.get("cache_read_input_tokens", 0) or 0),
                }
                # cumulativo di sessione (per la vista "Agents Activity")
                self._total_tokens["input"] += self._last_usage["input_tokens"]
                self._total_tokens["output"] += self._last_usage["output_tokens"]
                self._total_tokens["runs"] += 1
                await bus.publish(Event(
                    type="usage",
                    payload={"chat_id": self.chat_id, **self._last_usage},
                    timestamp=datetime.now(timezone.utc),
                ))
        return "".join(parts)

    async def _set_status(self, status: ClodiaStatus) -> None:
        self.status = status
        await bus.publish(Event(
            type="status",
            payload={"chat_id": self.chat_id, "status": status.value},
            timestamp=datetime.now(timezone.utc),
        ))

    async def _publish_error(self, message: str, reason: str = "") -> None:
        payload = {"chat_id": self.chat_id, "message": message}
        if reason:
            payload["reason"] = reason
        await bus.publish(Event(
            type="error",
            payload=payload,
            timestamp=datetime.now(timezone.utc),
        ))

    async def _record(self, msg: dict) -> None:
        self.last_activity = datetime.now(timezone.utc)
        entry = {**msg, "id": str(uuid.uuid4()), "timestamp": self.last_activity.isoformat()}
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        with self.session_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        # Auto-title: primo user message diventa titolo (prefisso kind + snippet 60 char)
        prefix = KIND_TITLE_PREFIX[self.kind]
        default_titles = ("", "Nuova chat", f"{prefix} Nuova chat")
        if msg.get("role") == "user" and self.title in default_titles:
            content = (msg.get("content") or "").strip().splitlines()[0] if msg.get("content") else ""
            snippet = content[:60] if content else "Nuova chat"
            self.title = f"{prefix} {snippet}"
        await bus.publish(Event(
            type="message",
            payload={"chat_id": self.chat_id, **entry},
            timestamp=datetime.now(timezone.utc),
        ))

    def read_history(self) -> list[dict]:
        if not self.session_file.is_file():
            return []
        out: list[dict] = []
        for line in self.session_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def to_dict(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "principal": getattr(self, "principal", None),
            "last_usage": self._last_usage or {},
            "total_tokens": self._total_tokens,
            "runtime": "claude",
        }


CODEX_BIN = os.environ.get("CODEX_BIN", "codex")


class CodexChatSession:
    """Chat servita dal runtime **codex** (ophelia = clodia-su-codex).

    Stessa interfaccia di ChatSession (start/send_user_message/stop/...): scrive
    sullo stesso event-bus e nella stessa history JSONL, quindi frontend, /chats
    e SSE restano identici. Il "trasporto" cambia: invece di un client SDK
    persistente, ogni turno esegue `codex exec` (`resume <thread_id>` dal secondo
    turno) con CODEX_HOME materializzato dall'abbonamento OpenAI. Gli eventi
    JSONL di codex sono tradotti negli stessi Event del Claude SDK.
    """

    def __init__(self, chat_id: str, kind: str = "ophelia", title: str = "") -> None:
        if not known_kind(kind):
            raise ValueError(f"unknown agent kind: {kind}")
        self.chat_id = chat_id
        self.kind = kind
        self.title = title or f"{_resolve_title_prefix(kind)} Nuova chat"
        self.status = ClodiaStatus.STOPPED
        self.created_at = datetime.now(timezone.utc)
        self.last_activity = self.created_at
        self._client = None            # sentinella "avviata" (None = stopped)
        self._lock = asyncio.Lock()
        self._current_turn_task: Optional[asyncio.Task] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._codex_home: Optional[Path] = None
        self._thread_id: Optional[str] = None   # session id codex per il resume
        self._last_usage: dict[str, int] = {}
        self._total_tokens: dict[str, int] = {"input": 0, "output": 0, "runs": 0}
        self._spawn = None                       # EphemeralWorkspace dello spawn
        self._spawn_dir: Optional[Path] = None   # cwd dello spawn (con AGENTS.md governato)
        # Utente UMANO della chat (principal verificato dal token webui). Per
        # codex il token gateway è coniato per-turno → sempre col principal corrente.
        self.principal: Optional[str] = None

    @property
    def cwd(self) -> Path:
        return _resolve_cwd(self.kind)

    @property
    def sessions_dir(self) -> Path:
        return _resolve_sessions_dir(self.kind)

    @property
    def session_file(self) -> Path:
        return self.sessions_dir / f"chat-{self.chat_id}.jsonl"

    @property
    def _thread_file(self) -> Path:
        return self.sessions_dir / f"chat-{self.chat_id}.thread"

    async def start(self) -> None:
        from ..api.providers import codex_home
        home = codex_home()
        if home is None:
            raise RuntimeError(
                "OpenAI (codex) non connesso: collega l'abbonamento nella sezione Providers")
        self._codex_home = Path(home)
        # Cabla il gateway clodia-tools come MCP HTTP per codex: config.toml con
        # url + bearer_token_env_var. Il token (col principal) è messo nell'env
        # ad ogni turno (vedi run) → ophelia ha gli stessi runtime.* di clodia.
        try:
            (self._codex_home / "config.toml").write_text(
                "[mcp_servers.clodia-tools]\n"
                f'url = "{CLODIA_TOOLS_MCP_URL}"\n'
                'bearer_token_env_var = "CLODIA_TOOLS_TOKEN"\n',
                encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            LOG.warning("config.toml MCP per codex non scritto: %s", e)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        # webchat = SPAWN: materializza il seed (costituzione + skill + memory +
        # scratch). Per codex l'AGENTS.md dello spawn include già la governance
        # (costituzione+identità), che codex auto-carica dalla cwd. I turni girano
        # nello scratch dello spawn. Lo spawn è pulito a stop().
        self._spawn, self._spawn_dir = _materialize_spawn(self.kind)
        if self._spawn_dir is None:
            # fallback (seed assente dal registry): governance nella cwd stabile
            self.cwd.mkdir(parents=True, exist_ok=True)
            self._write_seed_governance()
        # riprendi il thread_id codex se la chat è ripresa da disco
        if self._thread_file.is_file():
            self._thread_id = self._thread_file.read_text().strip() or None
        self._client = object()        # marca avviata (auto-restart check in agents.py)
        await self._set_status(ClodiaStatus.IDLE)
        intro = KIND_AUTO_INTRO.get(self.kind)
        if intro:
            asyncio.create_task(self._do_send_bg(intro))

    def _write_seed_governance(self) -> None:
        """Scrive in <cwd>/AGENTS.md la governance presa dal SEED dell'agent:
        costituzione (genoma, dal constitution-catalog) + identità (il
        system-prompt.md del seed). codex auto-carica AGENTS.md dalla cwd a ogni
        turno, quindi l'agent è governato dal proprio seed. Nessun Trello/kanban.
        Se il seed non è nel registry, lascia il comportamento legacy (no-op)."""
        try:
            text = _seed_governance_text(self.kind)
            if not text:
                return
            self.cwd.mkdir(parents=True, exist_ok=True)
            (self.cwd / "AGENTS.md").write_text(text, encoding="utf-8")
            LOG.info("governance dal seed scritta in %s/AGENTS.md (kind=%s)", self.cwd, self.kind)
        except Exception as e:  # noqa: BLE001 — il wiring del seed non deve impedire lo start
            LOG.warning("governance seed non applicata per %s: %s", self.kind, e)

    async def stop(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        self._proc = None
        self._client = None
        if self._spawn is not None:
            try:
                self._spawn.cleanup()
            except Exception:  # noqa: BLE001
                pass
            self._spawn = None
            self._spawn_dir = None
        await self._set_status(ClodiaStatus.STOPPED)

    async def send_user_message(self, content: str) -> str:
        if self._client is None:
            raise RuntimeError("session not started")
        async with self._lock:
            await self._record({"role": "user", "content": content})
            await self._set_status(ClodiaStatus.THINKING)
            self._last_usage = {}
            self._current_turn_task = asyncio.create_task(self._run_turn(content))
            try:
                full = await self._current_turn_task
                await self._record({"role": "assistant", "content": full})
                await self._set_status(ClodiaStatus.IDLE)
                return full
            except asyncio.CancelledError:
                note = "⏹ Inferenza interrotta dall'utente."
                await self._set_status(ClodiaStatus.CANCELLING)
                await self._record({"role": "system", "content": note})
                await bus.publish(Event(
                    type="interrupted",
                    payload={"chat_id": self.chat_id, "reason": "user_interrupt"},
                    timestamp=datetime.now(timezone.utc),
                ))
                await self._set_status(ClodiaStatus.IDLE)
                return note
            except Exception as e:
                await self._set_status(ClodiaStatus.ERROR)
                await self._record({"role": "system", "content": f"⚠ Errore codex: {e}"})
                await self._publish_error(str(e))
                raise
            finally:
                self._current_turn_task = None
                self._proc = None

    async def send_user_message_async(self, content: str) -> dict:
        if self._client is None:
            raise RuntimeError("session not started")
        asyncio.create_task(self._do_send_bg(content))
        return {"chat_id": self.chat_id, "queued": True}

    async def _do_send_bg(self, content: str) -> None:
        try:
            await self.send_user_message(content)
        except Exception:
            pass

    async def interrupt_current_turn(self) -> bool:
        task = self._current_turn_task
        if task is None or task.done():
            return False
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        task.cancel()
        return True

    async def _run_turn(self, content: str) -> str:
        activity_log.append(self.kind, "run_started",
                            {"prompt": _snippet(content), "principal": self.principal,
                             "chat_id": self.chat_id})
        cmd = [CODEX_BIN, "exec"]
        if self._thread_id:
            cmd += ["resume", self._thread_id]
        # niente -C: il workdir è già imposto via cwd= sul subprocess (e `resume`
        # non accetta -C). --skip-git-repo-check: il workspace non è un repo git.
        cmd += ["--json", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check"]
        env = {**os.environ, "CODEX_HOME": str(self._codex_home)}
        # token gateway coniato PER-TURNO col principal corrente (utente connesso)
        # → runtime.current_user resta sempre allineato senza restart.
        try:
            env["CLODIA_TOOLS_TOKEN"] = pki.mint_session_token(
                self.kind, ttl_seconds=_CLODIA_TOOLS_TOKEN_TTL, principal=self.principal)
        except Exception as e:  # noqa: BLE001
            LOG.warning("token clodia-tools (codex) non coniato per %s: %s", self.kind, e)
        run_cwd = str(self._spawn_dir or self.cwd)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env, cwd=run_cwd, limit=_STREAM_LIMIT,
        )
        self._proc = proc
        # prompt via stdin (evita parsing del prompt come argomento)
        try:
            proc.stdin.write(content.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass
        parts: list[str] = []
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            await self._handle_event(ev, parts)
        await proc.wait()
        if proc.returncode not in (0, None) and not parts:
            err = ""
            if proc.stderr is not None:
                err = (await proc.stderr.read()).decode(errors="replace")[:300]
            activity_log.append(self.kind, "error",
                                {"error": f"codex exit {proc.returncode}", "chat_id": self.chat_id})
            raise RuntimeError(f"codex exit {proc.returncode}: {err or 'nessun output'}")
        full = "".join(parts)
        activity_log.append(self.kind, "run_done",
                            {"reply": _snippet(full), "chat_id": self.chat_id,
                             "usage": self._last_usage or None})
        return full

    async def _handle_event(self, ev: dict, parts: list[str]) -> None:
        t = ev.get("type")
        if t == "thread.started":
            tid = ev.get("thread_id")
            if tid and tid != self._thread_id:
                self._thread_id = tid
                try:
                    self.sessions_dir.mkdir(parents=True, exist_ok=True)
                    self._thread_file.write_text(tid)
                except OSError:
                    pass
        elif t == "item.completed":
            item = ev.get("item") or {}
            itype = item.get("type")
            text = item.get("text") or ""
            if itype == "agent_message":
                if text:
                    parts.append(text)
                    await bus.publish(Event(
                        type="message_chunk",
                        payload={"chat_id": self.chat_id, "role": "assistant", "delta": text},
                        timestamp=datetime.now(timezone.utc),
                    ))
            elif itype == "reasoning":
                if text:
                    await bus.publish(Event(
                        type="thinking_chunk",
                        payload={"chat_id": self.chat_id, "delta": text},
                        timestamp=datetime.now(timezone.utc),
                    ))
            elif itype in ("command_execution", "mcp_tool_call", "local_shell_call",
                           "file_change", "web_search", "patch_apply"):
                summary = (item.get("command") or item.get("query")
                           or item.get("server") or item.get("name") or "")
                await bus.publish(Event(
                    type="tool_use",
                    payload={"chat_id": self.chat_id, "tool": itype,
                             "input_summary": str(summary)[:200]},
                    timestamp=datetime.now(timezone.utc),
                ))
        elif t == "turn.completed":
            u = ev.get("usage") or {}
            self._last_usage = {
                "input_tokens": int(u.get("input_tokens", 0) or 0),
                "output_tokens": int(u.get("output_tokens", 0) or 0),
                "cache_read_input_tokens": int(u.get("cached_input_tokens", 0) or 0),
            }
            await bus.publish(Event(
                type="usage",
                payload={"chat_id": self.chat_id, **self._last_usage},
                timestamp=datetime.now(timezone.utc),
            ))
        elif t in ("error", "turn.failed"):
            msg = ev.get("error") or ev.get("message") or json.dumps(ev)[:200]
            if isinstance(msg, dict):
                msg = msg.get("message") or json.dumps(msg)[:200]
            await self._publish_error(str(msg))

    async def _set_status(self, status: ClodiaStatus) -> None:
        self.status = status
        await bus.publish(Event(
            type="status",
            payload={"chat_id": self.chat_id, "status": status.value},
            timestamp=datetime.now(timezone.utc),
        ))

    async def _publish_error(self, message: str, reason: str = "") -> None:
        payload = {"chat_id": self.chat_id, "message": message}
        if reason:
            payload["reason"] = reason
        await bus.publish(Event(type="error", payload=payload,
                               timestamp=datetime.now(timezone.utc)))

    async def _record(self, msg: dict) -> None:
        self.last_activity = datetime.now(timezone.utc)
        entry = {**msg, "id": str(uuid.uuid4()), "timestamp": self.last_activity.isoformat()}
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        with self.session_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        prefix = KIND_TITLE_PREFIX[self.kind]
        default_titles = ("", "Nuova chat", f"{prefix} Nuova chat")
        if msg.get("role") == "user" and self.title in default_titles:
            content = (msg.get("content") or "").strip().splitlines()[0] if msg.get("content") else ""
            snippet = content[:60] if content else "Nuova chat"
            self.title = f"{prefix} {snippet}"
        await bus.publish(Event(
            type="message",
            payload={"chat_id": self.chat_id, **entry},
            timestamp=datetime.now(timezone.utc),
        ))

    def read_history(self) -> list[dict]:
        if not self.session_file.is_file():
            return []
        out: list[dict] = []
        for line in self.session_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def to_dict(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "principal": getattr(self, "principal", None),
            "last_usage": self._last_usage or {},
            "total_tokens": self._total_tokens,
            "runtime": "codex",
        }


class ChatManager:
    """Multi-chat: dict {chat_id → ChatSession}. Una chat 'default' al boot."""

    def __init__(self) -> None:
        self._chats: dict[str, "ChatSession | CodexChatSession"] = {}
        self._lock = asyncio.Lock()

    def list(self) -> list[ChatSession]:
        # Ordina per ultima attività decrescente
        return sorted(self._chats.values(), key=lambda c: c.last_activity, reverse=True)

    def get(self, chat_id: str) -> ChatSession:
        if chat_id not in self._chats:
            raise KeyError(chat_id)
        return self._chats[chat_id]

    async def create(self, chat_id: Optional[str] = None, kind: str = DEFAULT_KIND) -> ChatSession:
        async with self._lock:
            # Enforcement: un agent col provider scollegato non è disponibile —
            # né per chat (qui) né per job (fire_job passa di qui). Choke point unico.
            _ensure_provider_connected(kind)
            cid = chat_id or _new_chat_id()
            if cid in self._chats:
                raise ValueError(f"chat '{cid}' already exists")
            cls = CodexChatSession if _is_codex_kind(kind) else ChatSession
            chat = cls(cid, kind=kind)
            # Pre-popola titolo dalla history se esiste su disco
            existing = chat.read_history()
            if existing:
                first_user = next((m for m in existing if m.get("role") == "user"), None)
                if first_user:
                    content = (first_user.get("content") or "").strip().splitlines()[0]
                    prefix = _resolve_title_prefix(kind)
                    snippet = content[:60] if content else "Chat (ripresa)"
                    chat.title = f"{prefix} {snippet}"
                # Last activity dalla history
                last = existing[-1]
                try:
                    chat.last_activity = datetime.fromisoformat(last.get("timestamp"))
                except Exception:
                    pass
            self._chats[cid] = chat
        await chat.start()
        await bus.publish(Event(
            type="chat_created",
            payload=chat.to_dict(),
            timestamp=datetime.now(timezone.utc),
        ))
        return chat

    async def delete(self, chat_id: str) -> None:
        async with self._lock:
            if chat_id not in self._chats:
                raise KeyError(chat_id)
            chat = self._chats.pop(chat_id)
        await chat.stop()
        await bus.publish(Event(
            type="chat_deleted",
            payload={"chat_id": chat_id},
            timestamp=datetime.now(timezone.utc),
        ))


def _new_chat_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _extract_text(message) -> str:
    if hasattr(message, "content"):
        content = message.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            out = []
            for block in content:
                if isinstance(block, str):
                    out.append(block)
                elif hasattr(block, "text"):
                    out.append(block.text)
                elif isinstance(block, dict) and "text" in block:
                    out.append(block["text"])
            return "".join(out)
    if hasattr(message, "text"):
        return message.text
    if isinstance(message, str):
        return message
    return ""


def _summarize_input(inp: dict) -> str:
    if not inp:
        return ""
    for key in ("command", "path", "file_path", "query", "url", "content", "description"):
        if key in inp:
            val = str(inp[key])
            return val[:120]
    import json
    return json.dumps(inp, ensure_ascii=False)[:120]


def _content_preview(content, max_len: int) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:max_len]
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                return b.get("text", "")[:max_len]
    return str(content)[:max_len]


manager = ChatManager()
