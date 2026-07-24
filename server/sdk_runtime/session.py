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
import re
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

# Iniezioni del runtime Claude che NON sono risposta dell'assistente: quando
# una skill viene espansa, il CLI la streamma come blocco testo che inizia con
# questa sentinella — senza filtro finiva nel messaggio di canale (bug
# segnalato da Davide, 8 lug 2026: SKILL.md intera prima della risposta).
_INJECTION_SENTINELS = ("Base directory for this skill:",)
_SENTINEL_MAXLEN = max(len(x) for x in _INJECTION_SENTINELS)


class _BlockFilter:
    """Classifica i text-block dello stream: trattiene i primi byte di ogni
    blocco finché non può decidere se è un'iniezione (drop) o testo vero
    (keep + flush). `feed(index, text)` ritorna il testo da mostrare ORA
    (può essere vuoto mentre bufferizza); `end_block()` flusha il residuo."""

    def __init__(self) -> None:
        self._index: int | None = None
        self._buf = ""
        self._mode = "undecided"   # undecided | keep | drop
        self._emitted_any = False  # per il separatore fra blocchi tenuti

    def _decide(self) -> None:
        if any(self._buf.startswith(x) for x in _INJECTION_SENTINELS):
            self._mode = "drop"
        else:
            self._mode = "keep"

    def feed(self, index, text: str) -> str:
        out = ""
        if index != self._index:
            out += self.end_block()
            self._index = index
        if self._mode == "drop":
            return out
        if self._mode == "keep":
            return out + text
        self._buf += text
        if len(self._buf) >= _SENTINEL_MAXLEN or any(
                not x.startswith(self._buf[:len(x)]) for x in _INJECTION_SENTINELS
                if len(self._buf) < len(x)):
            self._decide()
            if self._mode == "keep":
                flushed = self._buf
                self._buf = ""
                if self._emitted_any:
                    flushed = "\n\n" + flushed
                self._emitted_any = True
                return out + flushed
            self._buf = ""
        return out

    def end_block(self) -> str:
        """Chiude il blocco corrente; ritorna l'eventuale residuo da mostrare."""
        residue = ""
        if self._mode == "undecided" and self._buf:
            self._decide()
            if self._mode == "keep":
                residue = ("\n\n" if self._emitted_any else "") + self._buf
                self._emitted_any = True
        self._buf = ""
        self._mode = "undecided"
        self._index = None
        return residue

COLLECT_MAX_SECONDS   = 4 * 60 * 60  # 4h hard cap assoluto
QUERY_TIMEOUT         = 90         # invio prompt al subprocess: oltre = client wedged → recovery
# Watchdog di turno: INDIPENDENTE da asyncio.timeout (che su certi hang del
# subprocess non riesce a cancellare la __anext__). Se per WATCHDOG_SILENCE
# secondi non arriva NESSUN evento SDK, chiude forzatamente il client → la
# lettura appesa erra e il turno termina/recupera, invece di restare bloccato.
WATCHDOG_SILENCE = int(os.environ.get("CLODIA_TURN_WATCHDOG_SILENCE", "180"))
WATCHDOG_TICK    = 15
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

# ── Contenimento runtime (M3, opt-in) ────────────────────────────────────────
# Esegue il subprocess dell'SDK come utente NON-root, così il suo bash non può
# leggere i segreti root-only (ca.key/identity.key/vault). L'orchestrator resta
# root e conia i token. Modello Unix "famiglia/individuo":
#   uid UNICO per SPAWN  → isola ogni istanza viva (scratch 700 privato).
#   gid STABILE per SEED → istanze dello stesso seed condividono il "gruppo" del
#                          seed (per dati di seed condivisi via g+r); seed diversi
#                          restano isolati fra loro.
#   CLODIA_AGENT_SANDBOX_UID   = BASE del pool uid (>0 abilita). Vuoto/0 = OFF.
#   CLODIA_AGENT_SANDBOX_KINDS = CSV dei kind, o "*" per tutti.
import threading as _threading
import zlib as _zlib

_SANDBOX_UID_BASE = int(os.environ.get("CLODIA_AGENT_SANDBOX_UID", "0") or "0")
_SANDBOX_UID_SPAN = int(os.environ.get("CLODIA_AGENT_SANDBOX_UID_SPAN", "2000"))
_SANDBOX_GID_BASE = (_SANDBOX_UID_BASE + _SANDBOX_UID_SPAN) if _SANDBOX_UID_BASE else 0
_SANDBOX_GID_SPAN = 1000
_SANDBOX_KINDS = {
    k.strip() for k in os.environ.get("CLODIA_AGENT_SANDBOX_KINDS", "").split(",") if k.strip()
}
_SANDBOX_WRAPPER = os.environ.get(
    "CLODIA_AGENT_SANDBOX_WRAPPER", "/clodia/docker/agent-sandbox-exec.sh")
_uid_lock = _threading.Lock()
_uids_in_use: set[int] = set()


def _sandbox_enabled(kind: str) -> bool:
    return _SANDBOX_UID_BASE > 0 and (kind in _SANDBOX_KINDS or "*" in _SANDBOX_KINDS)


def _seed_gid(kind: str) -> int:
    """gid STABILE per seed: stesso valore per tutte le istanze del seed."""
    return _SANDBOX_GID_BASE + (_zlib.crc32(kind.encode()) % _SANDBOX_GID_SPAN)


def _alloc_uid() -> int:
    """Alloca un uid libero dal pool per QUESTO spawn (isolamento per-istanza)."""
    with _uid_lock:
        for u in range(_SANDBOX_UID_BASE, _SANDBOX_UID_BASE + _SANDBOX_UID_SPAN):
            if u not in _uids_in_use:
                _uids_in_use.add(u)
                return u
    raise RuntimeError("nessun uid sandbox libero nel pool")


def _free_uid(u: Optional[int]) -> None:
    if u is None:
        return
    with _uid_lock:
        _uids_in_use.discard(int(u))


def _bundled_cli_path() -> str:
    import claude_agent_sdk as _sdk
    return os.path.join(os.path.dirname(_sdk.__file__), "_bundled", "claude")

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


def _spawn_identity(spawn) -> dict:
    if spawn is None:
        return {"spawn_id": None, "spawn_instance": None}
    name = getattr(getattr(spawn, "dir", None), "name", None)
    if not name:
        return {"spawn_id": None, "spawn_instance": None}
    _agent, sep, instance = name.rpartition("-")
    return {"spawn_id": name, "spawn_instance": instance if sep else None}


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


def _kind_clearance(kind: str) -> Optional[str]:
    """Clearance MINIMA dichiarata dall'agent in agent.yaml (floor di deployment:
    la SEAL minima che il suo provider deve avere). None se non dichiarata. NON è
    la clearance effettiva: quella è SEMPRE il SEAL del provider effettivo
    (`_effective_clearance`) — il seed non è un tetto e non la riduce."""
    spec = _kind_spec(kind)
    return getattr(spec, "clearance", None) if spec else None


def _seal_num(s: Optional[str]) -> Optional[int]:
    try:
        return int(str(s).replace("SEAL-", "").strip()) if s else None
    except Exception:  # noqa: BLE001
        return None


def _effective_clearance(kind: str) -> Optional[str]:
    """Clearance EFFETTIVA (per il token) = SEAL del PROVIDER effettivo su cui gira
    l'agente. Il SEAL di un agente NON è statico né definito dal seed: dipende dal
    provider, perché il dato va lì — impiegato su Scaleway (SEAL-3) è SEAL-3, su
    anthropic-api (SEAL-1) è SEAL-1. Il campo `clearance` del seed è solo una SEAL
    MINIMA dichiarata (floor), NON un tetto: non riduce l'effettiva. Vale per
    TUTTI, super inclusi (clodia/ophelia): nessuno tratta dati SEAL-3+ su provider
    SEAL-2-. Provider non risolto → fallback alla minima dichiarata."""
    try:
        from ..api.providers import provider_seal
        prov = agent_effective_provider(kind)
        ps = provider_seal(prov) if prov else None
    except Exception as e:  # noqa: BLE001
        LOG.warning("provider_seal non risolto per kind=%s: %s", kind, e)
        ps = None
    if ps:
        n = _seal_num(ps)
        return f"SEAL-{n}" if n is not None else ps
    return _kind_clearance(kind)  # fallback: la minima dichiarata dal seed


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
                                       getattr(spec, "provider", None), spec.agent_sdk,
                                       getattr(spec, "model", None),
                                       getattr(spec, "provider_models", None))
        sdk = "codex" if kind in CODEX_KINDS else "claude"
        return default_providers_for_sdk(sdk)
    except Exception:  # noqa: BLE001
        return []


def agent_effective_provider(kind: str) -> Optional[str]:
    """Provider EFFETTIVO del kind: override manuale (selezione dal profilo, se
    usabile) altrimenti il primo attivo (connesso e non in pausa) nell'ordine di
    preferenza dichiarato, fra quelli che servono il modello dell'agent. None se
    nessuno. Fail-open al preferito su errore infra."""
    try:
        from ..api.providers import effective_provider, connected_provider_ids, provider_override
        connected = connected_provider_ids()
    except Exception:  # noqa: BLE001 — fail-open su errore infra
        return (agent_candidates(kind) or [None])[0]
    ov = provider_override(kind)  # selezione manuale dal profilo agent (o None)
    spec = _kind_spec(kind)
    if spec is not None:
        return effective_provider(getattr(spec, "providers", None),
                                  getattr(spec, "provider", None), spec.agent_sdk,
                                  connected, getattr(spec, "model", None), override=ov,
                                  provider_models=getattr(spec, "provider_models", None))
    sdk = "codex" if kind in CODEX_KINDS else "claude"
    return effective_provider(None, None, sdk, connected, None, override=ov)


def agent_provider(kind: str) -> Optional[str]:
    """Compat: provider effettivo, o (se nessuno collegato) il preferito."""
    return agent_effective_provider(kind) or (agent_candidates(kind) or [None])[0]


def agent_effective_model(kind: str) -> Optional[str]:
    """Modello che l'agent userà EFFETTIVAMENTE, coerente col provider effettivo:
    override per-provider (`provider_models[provider]`) se presente, altrimenti il
    `model` dichiarato; poi tradotto in inference-profile se il provider è Bedrock.
    Usato dai runtime (claude/opencode) per passare il model id giusto."""
    model = _resolve_model(kind)
    prov = agent_effective_provider(kind)
    spec = _kind_spec(kind)
    pm = getattr(spec, "provider_models", None) or {} if spec else {}
    if prov and prov in pm:
        model = pm[prov]
    try:
        from ..api.providers import bedrock_model_id
        bid = bedrock_model_id(prov, model)
        if bid:
            model = bid
    except Exception:  # noqa: BLE001
        pass
    return model


def agent_runtime_sdk(kind: str) -> str:
    """SDK del RUNTIME per il kind = SDK del provider EFFETTIVO (permette catene
    di fallback cross-SDK: scaleway→opencode, aws-region-eu→claude). Fallback:
    CODEX_KINDS statici → codex, altrimenti l'`agent_sdk` dichiarato, altrimenti
    claude. Robusto: se il provider non risolve, usa la dichiarazione dell'agent."""
    if kind in CODEX_KINDS:
        return "codex"
    try:
        from ..api.providers import provider_sdk
        prov = agent_effective_provider(kind)
        sdk = provider_sdk(prov)
        if sdk:
            return sdk
    except Exception:  # noqa: BLE001
        pass
    spec = _kind_spec(kind)
    return (getattr(spec, "agent_sdk", None) or "claude") if spec else "claude"


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
        self._last_event_at: float = 0.0   # ts ultimo evento SDK del turno (per il watchdog)
        self._watchdog_fired: bool = False  # il watchdog ha ucciso il subprocess di questo turno
        self._last_usage: dict[str, int] = {}
        self._total_tokens: dict[str, int] = {"input": 0, "output": 0, "runs": 0}
        # occupazione ATTUALE della finestra di contesto (token dell'ultimo turno).
        self._context_tokens: int = 0
        self._spawn = None  # EphemeralWorkspace dello spawn webchat (cleanup a stop)
        self._sandbox_uid: Optional[int] = None  # uid per-spawn allocato (sandbox)
        # Opzioni del client SDK calcolate in start(): riusate dal recovery per
        # ricreare il subprocess dopo un fallimento senza ricalcolare env/spawn.
        self._opts_kwargs: Optional[dict] = None
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
        # Segreti SOLO-orchestrator: mai nel child-env di uno spawn (un agent via
        # bash farebbe `env` e li leggerebbe). Il secret di bootstrap del minting è
        # la chiave per farsi coniare identità arbitrarie dal gateway → esporlo
        # vanificherebbe il contenimento. GIT_TOKEN = PAT git, non compete a un
        # agent sandboxato (le operazioni git passano dal gateway).
        for _sk in ("CLODIA_ORCHESTRATOR_SECRET", "GIT_TOKEN"):
            child_env.pop(_sk, None)
        # Mutua esclusione provider: rimuovi dall'env EREDITATO tutte le
        # credenziali provider note (ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN,
        # OPENAI_API_KEY). Senza questo, una chiave globale/residua nel container
        # (es. ANTHROPIC_API_KEY a consumo) oscura un agent assegnato a un altro
        # provider (es. OAuth abbonamento): il CLI preferisce la API key e ignora
        # ~/.claude/. Subito dopo iniettiamo SOLO la credenziale del provider
        # EFFETTIVO dell'agent. Il valore non transita mai dal modello.
        try:
            from ..api.providers import provider_env, all_provider_env_keys
            for _pk in all_provider_env_keys():
                child_env.pop(_pk, None)
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
        # Modello EFFETTIVO: override per-provider (provider_models) se presente,
        # altrimenti quello dichiarato; su Bedrock tradotto nell'inference-profile
        # EU (claude-sonnet-4-5 → eu.anthropic.claude-sonnet-4-6). No-op sui
        # provider non-Bedrock / senza override.
        model_override = agent_effective_model(self.kind)
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
                                              principal=self.principal,
                                              clearance=_effective_clearance(self.kind), chat=self.chat_id)
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
        # Contenimento runtime (opt-in per-kind): fa girare il CLI come non-root
        # via wrapper. uid UNICO per questo spawn (isola l'istanza), gid del SEED
        # (famiglia). Lo scratch è chownato uid:gid mode 700 → privato dell'istanza.
        if _sandbox_enabled(self.kind) and spawn_dir is not None:
            uid = _alloc_uid()
            gid = _seed_gid(self.kind)
            self._sandbox_uid = uid
            opts_kwargs["cli_path"] = _SANDBOX_WRAPPER
            child_env["CLODIA_AGENT_UID"] = str(uid)
            child_env["CLODIA_AGENT_GID"] = str(gid)
            child_env["CLODIA_REAL_CLI"] = _bundled_cli_path()
            child_env["HOME"] = str(spawn_dir)  # HOME scrivibile dal non-root
            try:
                import subprocess as _sp
                # lo spawn è di proprietà del solo uid dell'istanza, modo 700
                _sp.run(["chown", "-R", f"{uid}:{gid}", str(spawn_dir)], check=False)
                _sp.run(["chmod", "-R", "700", str(spawn_dir)], check=False)
                LOG.info("sandbox runtime kind=%s uid=%s gid=%s (spawn 700)", self.kind, uid, gid)
            except Exception as e:  # noqa: BLE001
                LOG.warning("chown/chmod spawn per sandbox fallito (kind=%s): %s", self.kind, e)
        self._opts_kwargs = opts_kwargs
        await self._open_client()
        # Auto-intro fire-and-forget: se il kind ne ha uno definito, lo
        # consegnamo come primo messaggio user in background. Il caller di
        # start() ritorna subito; eventuali messaggi successivi dell'operatore
        # vengono serializzati dal lock interno della sessione.
        intro = KIND_AUTO_INTRO.get(self.kind)
        if intro:
            asyncio.create_task(self._do_send_bg(intro))

    async def _open_client(self) -> None:
        """(Ri)apre il client SDK dalle opzioni già calcolate in start().
        Usato sia all'avvio sia dal recovery: crea il subprocess claude e
        riporta la sessione a IDLE (pronta)."""
        options = ClaudeAgentOptions(**self._opts_kwargs)
        self._client_ctx = ClaudeSDKClient(options=options)
        self._client = await self._client_ctx.__aenter__()
        await self._set_status(ClodiaStatus.IDLE)

    def _refresh_provider_env(self) -> bool:
        """Aggiorna in-place l'env del provider effettivo nelle opzioni del
        client. `provider_env` rinnova il token OAuth se scaduto/in scadenza (e
        lo persiste). Necessario perché il subprocess è long-lived ma il token
        viene iniettato statico allo start: senza questo, dopo qualche ora il
        token scade e ogni turno dà 401 finché non si riavvia. Ritorna True se
        il token è cambiato (→ il client va riaperto per iniettarlo)."""
        if self._opts_kwargs is None:
            return False
        try:
            from ..api.providers import provider_env
            eff = agent_effective_provider(self.kind)
            if not eff:
                return False
            fresh = provider_env(eff)
        except Exception as e:  # noqa: BLE001 — un refresh fallito non deve rompere il turno
            LOG.warning("refresh provider env fallito per kind=%s: %s", self.kind, e)
            return False
        if not fresh:
            return False  # provider scollegato: non azzerare l'env esistente
        env = self._opts_kwargs.setdefault("env", {})
        changed = any(env.get(k) != v for k, v in fresh.items())
        if changed:
            env.update(fresh)
        return changed

    def _refresh_mcp_principal(self) -> bool:
        """Se il principal umano del turno è cambiato rispetto a quello 'cotto'
        nel token MCP (all'avvio è None), ri-conia il token con il principal
        attuale e aggiorna gli header di `mcp_servers` → così il gateway vede
        l'UMANO reale del turno (enforcement compartimento need-to-know), non
        None. Ritorna True se cambiato (→ il client va riaperto per iniettarlo).
        Nel flusso normale scatta UNA volta, al primo turno umano (None→utente):
        contesto vuoto, nessuna perdita. Chiude il gap descritto a `_open_client`."""
        if self._opts_kwargs is None or self.principal == self._token_principal:
            return False
        mcp = (self._opts_kwargs.get("mcp_servers") or {}).get("clodia-tools")
        if not isinstance(mcp, dict) or "headers" not in mcp:
            return False  # kind senza MCP clodia-tools
        try:
            ct_token = pki.mint_session_token(
                self.kind, ttl_seconds=_CLODIA_TOOLS_TOKEN_TTL,
                principal=self.principal, clearance=_effective_clearance(self.kind), chat=self.chat_id)
        except Exception as e:  # noqa: BLE001 — un re-mint fallito non rompe il turno
            LOG.warning("re-mint token MCP (principal) fallito per kind=%s: %s", self.kind, e)
            return False
        mcp["headers"]["Authorization"] = f"Bearer {ct_token}"
        self._token_principal = self.principal
        return True

    async def _recover_session(self) -> bool:
        """Dopo un fallimento del turno (errore, timeout o subprocess wedged)
        riporta la sessione a uno stato PRONTO: chiude il client SDK corrente
        — potenzialmente bloccato o morto — e lo ricrea. Così il messaggio
        successivo parte su un client sano invece di accodarsi a uno appeso:
        è quest'ultimo il vero motivo per cui un canale restava "bloccato"
        finché non si ricreava il container. Best-effort: se il restart
        fallisce lo status resta ERROR, ma il lock è comunque già rilasciato
        dal chiamante (`async with self._lock`), quindi niente deadlock."""
        if self._opts_kwargs is None:
            return False
        # un fallimento può essere un 401 da token scaduto: rinnova prima di riaprire
        self._refresh_provider_env()
        try:
            if self._client_ctx is not None:
                try:
                    await asyncio.wait_for(
                        self._client_ctx.__aexit__(None, None, None), timeout=10)
                except (Exception, asyncio.TimeoutError):
                    pass  # subprocess già morto/wedged: si procede comunque
            self._client = None
            self._client_ctx = None
            await self._open_client()
            LOG.info("sessione %s ripristinata e pronta dopo fallimento turno", self.chat_id)
            return True
        except Exception as e:  # noqa: BLE001
            LOG.error("recovery sessione %s fallita: %s", self.chat_id, e)
            return False

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
        # rilascia l'uid per-spawn al pool (sandbox)
        if self._sandbox_uid is not None:
            _free_uid(self._sandbox_uid)
            self._sandbox_uid = None
        await self._set_status(ClodiaStatus.STOPPED)

    async def send_user_message(self, content: str) -> str:
        if self._client is None:
            raise RuntimeError("session not started")
        async with self._lock:
            # Token OAuth long-lived: se è in scadenza, provider_env lo rinnova;
            # se è cambiato riapro il client col token fresco PRIMA del turno —
            # così un subprocess di vecchia data non dà 401 a metà sessione.
            # Riapri il client se cambia il token provider (scadenza OAuth) O il
            # principal umano del turno (per propagarlo al gateway MCP). Uso `|`
            # (non `or`) così entrambe le refresh girano sempre.
            if self._refresh_provider_env() | self._refresh_mcp_principal():
                LOG.info("token rinnovato (provider/principal) → riapro il client per %s", self.chat_id)
                await self._recover_session()
            await self._record({"role": "user", "content": content})
            await self._set_status(ClodiaStatus.THINKING)
            activity_log.append(self.kind, "run_started",
                                {"prompt": _snippet(content), "principal": self.principal,
                                 "chat_id": self.chat_id})
            LOG.info("turno START %s: %s", self.chat_id, _snippet(content, 80))
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
                        # timeout sull'invio: un client wedged non deve appendere
                        # il lock (e con esso ogni messaggio successivo) all'infinito.
                        async with asyncio.timeout(QUERY_TIMEOUT):
                            await self._client.query(content)
                        LOG.info("turno %s: query inviata, raccolgo la risposta", self.chat_id)
                    except Exception as e:
                        LOG.error("turno %s: query fallita/timeout: %s", self.chat_id, e)
                        activity_log.append(self.kind, "error",
                                            {"error": _snippet(str(e)), "chat_id": self.chat_id})
                        await self._publish_error(str(e))
                        if not await self._recover_session():
                            await self._set_status(ClodiaStatus.ERROR)
                        raise
                    self._last_event_at = asyncio.get_event_loop().time()
                    self._watchdog_fired = False
                    self._current_turn_task = asyncio.create_task(self._collect_response())
                    _watchdog = asyncio.create_task(self._turn_watchdog(self._current_turn_task))
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
                                             "usage": self._last_usage or None,
                             "provider": agent_effective_provider(self.kind)})
                        await self._set_status(ClodiaStatus.IDLE)
                        return full
                    except asyncio.CancelledError:
                        # distingui interruzione utente da kill del watchdog
                        wd = self._watchdog_fired
                        note = ("⏱ Turno interrotto dal watchdog: il subprocess non rispondeva "
                                "(nessun evento per troppo tempo). Riprova."
                                if wd else "⏹ Inferenza interrotta dall'utente.")
                        reason = "watchdog_kill" if wd else "user_interrupt"
                        generation.update(output=trace_io(note),
                                          metadata={"status": "watchdog" if wd else "interrupted"})
                        await self._set_status(ClodiaStatus.CANCELLING)
                        await self._record({"role": "system", "content": note})
                        await bus.publish(Event(
                            type="interrupted",
                            payload={"chat_id": self.chat_id, "reason": reason},
                            timestamp=datetime.now(timezone.utc),
                        ))
                        await self._set_status(ClodiaStatus.IDLE)
                        return note
                    except asyncio.TimeoutError:
                        note = (f"⏱ Timeout: nessun evento SDK per {COLLECT_CHUNK_TIMEOUT // 60}min "
                                f"(o superato il cap di {COLLECT_MAX_SECONDS // 3600}h).")
                        generation.update(output=trace_io(note), metadata={"status": "timeout"})
                        await self._record({"role": "system", "content": note})
                        await self._publish_error(note, reason="collect_timeout")
                        # client probabilmente wedged sul turno scaduto: ricrealo
                        # così il prossimo messaggio non si appende.
                        if not await self._recover_session():
                            await self._set_status(ClodiaStatus.ERROR)
                        raise
                    except Exception as e:
                        generation.update(output=trace_io(str(e)), metadata={"status": "error"})
                        await self._publish_error(str(e))
                        if not await self._recover_session():
                            await self._set_status(ClodiaStatus.ERROR)
                        raise
                    finally:
                        _watchdog.cancel()
                        self._current_turn_task = None
                        # se il watchdog ha ucciso il client, rimetti su una
                        # sessione PRONTA (altrimenti il turno dopo trova _client=None)
                        if self._watchdog_fired and self._client is None:
                            await self._recover_session()

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

    async def _turn_watchdog(self, turn_task: "asyncio.Task") -> None:
        """Watchdog del turno, indipendente da asyncio.timeout. Se per
        WATCHDOG_SILENCE secondi non arriva NESSUN evento SDK, chiude
        forzatamente il client (uccide il subprocess claude): la lettura appesa
        erra → il turno termina e il chiamante recupera. Risolve gli hang in cui
        il timeout async non scatta (la __anext__ non cede mai all'event loop)."""
        try:
            while not turn_task.done():
                await asyncio.sleep(WATCHDOG_TICK)
                if turn_task.done():
                    return
                silence = asyncio.get_event_loop().time() - self._last_event_at
                if silence >= WATCHDOG_SILENCE:
                    LOG.error("watchdog %s: nessun evento SDK da %.0fs → chiudo il subprocess",
                              self.chat_id, silence)
                    self._watchdog_fired = True
                    ctx = self._client_ctx
                    self._client = None
                    self._client_ctx = None
                    if ctx is not None:
                        try:
                            await asyncio.wait_for(ctx.__aexit__(None, None, None), timeout=15)
                        except Exception:  # noqa: BLE001 — subprocess che ignora il terminate
                            pass
                    # se la chiusura del client non ha sbloccato la read, forza il cancel
                    if not turn_task.done():
                        turn_task.cancel()
                    return
        except asyncio.CancelledError:
            pass

    async def _collect_response(self) -> str:
        parts: list[str] = []
        saw_text_delta = False
        blockfilter = _BlockFilter()
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

            self._last_event_at = asyncio.get_event_loop().time()  # progresso → watchdog quieto
            if isinstance(message, StreamEvent):
                # Delta token-by-token (include_partial_messages=True). L'evento
                # raw dell'API è in message.event: content_block_delta porta
                # delta.type = text_delta | thinking_delta. Li ritrasmettiamo
                # come *append* (campo `delta`) così il FE costruisce la bolla
                # progressivamente senza clobber multi-blocco.
                ev = message.event or {}
                if ev.get("type") == "content_block_stop":
                    residue = blockfilter.end_block()
                    if residue:
                        parts.append(residue)
                        saw_text_delta = True
                        await bus.publish(Event(
                            type="message_chunk",
                            payload={"chat_id": self.chat_id, "role": "assistant",
                                     "delta": residue},
                            timestamp=datetime.now(timezone.utc),
                        ))
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta" and delta.get("text"):
                        # Filtro iniezioni (SKILL.md espansa dal runtime) +
                        # separatore fra blocchi distinti: vedi _BlockFilter.
                        visible = blockfilter.feed(ev.get("index"), delta["text"])
                        if visible:
                            parts.append(visible)
                            saw_text_delta = True
                            await bus.publish(Event(
                                type="message_chunk",
                                payload={"chat_id": self.chat_id, "role": "assistant",
                                         "delta": visible},
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
                # Se lo SDK ha già emesso text_delta, `parts` contiene il testo
                # visibile in UI. Alcune versioni non ripetono quel testo nel
                # messaggio finale: affidarsi solo ad AssistantMessage può
                # produrre una risposta vuota nel canale pur avendo visto i
                # chunk live. Se invece non abbiamo delta, usiamo il fallback
                # testuale del messaggio completo.
                text = _extract_text(message)
                if text and not saw_text_delta:
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
                # UserMessage = tool result / echo dell'utente: MAI parte del
                # messaggio visibile dell'assistente. `continue` per non cadere nel
                # catch-all difensivo sotto, che estraeva il testo del tool-result
                # (es. il corpo di una skill caricata: "Base directory for this
                # skill: …") e lo appendeva alla risposta. Leak confermato via
                # strumentazione (msgtype=UserMessage, path catch-all).
                continue
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
                # occupazione finestra = prompt totale dell'ULTIMO turno (input + cache)
                self._context_tokens = (self._last_usage["input_tokens"]
                                        + self._last_usage["cache_creation_input_tokens"]
                                        + self._last_usage["cache_read_input_tokens"])
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
        prefix = _resolve_title_prefix(self.kind)
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
            "context_tokens": getattr(self, "_context_tokens", 0),
            "total_tokens": self._total_tokens,
            "runtime": "claude",
            **_spawn_identity(self._spawn),
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
        # occupazione ATTUALE della finestra di contesto (token dell'ultimo turno).
        self._context_tokens: int = 0
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
                self.kind, ttl_seconds=_CLODIA_TOOLS_TOKEN_TTL, principal=self.principal,
                clearance=_effective_clearance(self.kind), chat=self.chat_id)
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
        # Codex (`exec resume`) riporta l'usage CUMULATIVO del thread: registra il
        # DELTA di QUESTO run, così la leaderboard può sommare i run_done senza
        # multi-contare (bug totali provider nella pagina Activity).
        run_usage = self._codex_run_usage_delta(self._last_usage)
        # occupazione finestra = prompt dell'ULTIMO turno (il DELTA, non il cumulativo
        # del thread che Codex riporta): input + cache di questo run.
        if run_usage:
            self._context_tokens = (int(run_usage.get("input_tokens", 0) or 0)
                                    + int(run_usage.get("cache_read_input_tokens", 0) or 0))
        activity_log.append(self.kind, "run_done",
                            {"reply": _snippet(full), "chat_id": self.chat_id,
                             "usage": run_usage or None,
                             "provider": agent_effective_provider(self.kind)})
        return full

    def _codex_run_usage_delta(self, cumulative: dict | None) -> dict | None:
        """Codex riporta l'usage CUMULATIVO del thread (`exec resume`). Ritorna il
        DELTA di questo run rispetto al cumulativo del run precedente e aggiorna il
        baseline di sessione. Cumulativo che cala (thread ripartito) → baseline
        azzerato. usage vuoto (nessun turn.completed) → None, baseline invariato."""
        if not cumulative:
            return None
        keys = ("input_tokens", "output_tokens", "cache_read_input_tokens")
        base = getattr(self, "_usage_cumulative", None) or {}
        cur = {k: int(cumulative.get(k, 0) or 0) for k in keys}
        if cur["input_tokens"] < int(base.get("input_tokens", 0) or 0):
            base = {}  # thread ripartito: il cumulativo è calato → riparti da 0
        delta = {k: max(0, cur[k] - int(base.get(k, 0) or 0)) for k in keys}
        self._usage_cumulative = cur
        return delta

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
        prefix = _resolve_title_prefix(self.kind)
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
            "context_tokens": getattr(self, "_context_tokens", 0),
            "total_tokens": self._total_tokens,
            "runtime": "codex",
            **_spawn_identity(self._spawn),
        }


OPENCODE_BIN = os.environ.get("OPENCODE_BIN", "opencode")
# Timeout (s) di lettura del turno opencode `/message`. Un modello reasoning
# verboso (es. glm-5.2) su task complessi può NON convergere e iterare finché
# scade il read → la sessione resta "bloccata". Un cap più basso fa fallire in
# fretta (fail-fast) con errore chiaro, e al timeout abortiamo il turno lato
# opencode così la generazione runaway si ferma. Configurabile via env.
_OPENCODE_TURN_TIMEOUT = float(os.environ.get("OPENCODE_TURN_TIMEOUT", "180"))


class OpenCodeChatSession:
    """Chat servita dal runtime **OpenCode** (agent multi-provider, es. Messaggero
    su gpt-oss-120b/scaleway). Stessa interfaccia di ChatSession/CodexChatSession
    (start/send_user_message/stop/…): stesso event-bus e stessa history JSONL.

    Trasporto: un `opencode serve` per-sessione (porta effimera) + HTTP API.
    Ogni turno = POST /session/{ocid}/message (sincrono → `parts`), tradotti negli
    stessi Event. Provider e MCP (gateway clodia-tools) sono cablati in un
    `opencode.json` scritto nella cwd dello spawn; le credenziali passano via env
    (`{env:…}`) così non finiscono su disco. Vedi project_opencode_runtime_spike.
    """

    def __init__(self, chat_id: str, kind: str = "messaggero", title: str = "") -> None:
        if not known_kind(kind):
            raise ValueError(f"unknown agent kind: {kind}")
        self.chat_id = chat_id
        self.kind = kind
        self.title = title or f"{_resolve_title_prefix(kind)} Nuova chat"
        self.status = ClodiaStatus.STOPPED
        self.created_at = datetime.now(timezone.utc)
        self.last_activity = self.created_at
        self._client = None            # sentinella "avviata"
        self._lock = asyncio.Lock()
        self._current_turn_task: Optional[asyncio.Task] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._port: Optional[int] = None
        self._base_url: Optional[str] = None
        self._oc_session: Optional[str] = None   # id sessione OpenCode (per il resume)
        self._provider: Optional[str] = None
        self._model: Optional[str] = None
        self._last_usage: dict[str, int] = {}
        self._total_tokens: dict[str, int] = {"input": 0, "output": 0, "runs": 0}
        # occupazione ATTUALE della finestra di contesto (token dell'ultimo turno).
        self._context_tokens: int = 0
        self._spawn = None
        self._spawn_dir: Optional[Path] = None
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
    def _ocsession_file(self) -> Path:
        return self.sessions_dir / f"chat-{self.chat_id}.ocsession"

    def _free_port(self) -> int:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    def _write_config(self, cwd: Path) -> dict:
        """Scrive <cwd>/opencode.json (provider + MCP gateway) e ritorna l'env con
        le credenziali (referenziate via {env:…} nel config, così non su disco)."""
        self._provider = agent_effective_provider(self.kind)
        self._model = agent_effective_model(self.kind)
        env = {**os.environ}
        cfg: dict = {"$schema": "https://opencode.ai/config.json", "provider": {}, "mcp": {}}
        # credenziale del provider effettivo (apikey provider, es. scaleway)
        try:
            from ..api.providers import _read, provider_extra_env
            bundle = _read(self._provider) or {}
            key = bundle.get("api_key")
            opts: dict = {}
            if key:
                env["OPENCODE_PROVIDER_KEY"] = key
                opts["apiKey"] = "{env:OPENCODE_PROVIDER_KEY}"
            base = (provider_extra_env(self._provider) or {}).get("OPENAI_BASE_URL")
            if base:
                opts["baseURL"] = base
            # reasoning_effort del seed (es. glm-5.2 "none" → niente reasoning:
            # turni rapidi, no runaway). Passato nelle options del provider
            # opencode; ignorato dai modelli che non lo supportano.
            _spec = _kind_spec(self.kind)
            _reff = getattr(_spec, "reasoning_effort", None) if _spec else None
            if _reff:
                opts["reasoning_effort"] = _reff
            if opts:
                cfg["provider"][self._provider] = {"options": opts}
        except Exception as e:  # noqa: BLE001
            LOG.warning("opencode: credenziale provider %s non risolta: %s", self._provider, e)
        # gateway clodia-tools come MCP via bridge stdio `mcp-remote`. NB: il tipo
        # `remote` nativo di opencode si appende contro lo StreamableHTTP stateless
        # del gateway; `mcp-remote` (stdio↔HTTP) invece funziona. Serve --allow-http
        # perché l'endpoint interno è http:// (rete docker, non esposto). Il token
        # (col principal + clearance) va nell'header del bridge.
        try:
            tok = pki.mint_session_token(self.kind, ttl_seconds=_CLODIA_TOOLS_TOKEN_TTL,
                                         principal=self.principal,
                                         clearance=_effective_clearance(self.kind), chat=self.chat_id)
            cfg["mcp"]["clodia-tools"] = {
                "type": "local",
                "command": ["npx", "-y", "mcp-remote", CLODIA_TOOLS_MCP_URL,
                            "--header", f"Authorization: Bearer {tok}",
                            "--transport", "http-only", "--allow-http"],
                "enabled": True}
        except Exception as e:  # noqa: BLE001
            LOG.warning("opencode: MCP clodia-tools non cablato per %s: %s", self.kind, e)
        cwd.mkdir(parents=True, exist_ok=True)
        (cwd / "opencode.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        return env

    async def _http(self):
        import httpx
        return httpx.AsyncClient(base_url=self._base_url,
                                 timeout=httpx.Timeout(_OPENCODE_TURN_TIMEOUT))

    async def _abort_oc(self) -> None:
        """Abort best-effort del turno opencode in corso (ferma una generazione
        runaway lato serve). Idempotente e silenzioso."""
        if not (self._base_url and self._oc_session):
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as c:
                await c.post(f"{self._base_url}/session/{self._oc_session}/abort")
        except Exception:  # noqa: BLE001
            pass

    async def _wait_ready(self) -> None:
        import httpx
        for _ in range(60):  # ~30s
            try:
                async with httpx.AsyncClient(timeout=2.0) as c:
                    r = await c.get(f"{self._base_url}/doc")
                    if r.status_code == 200:
                        return
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)
        raise RuntimeError("opencode serve non pronto entro il timeout")

    async def start(self) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._spawn, self._spawn_dir = _materialize_spawn(self.kind)
        run_cwd = Path(self._spawn_dir or self.cwd)
        if self._spawn_dir is None:
            run_cwd.mkdir(parents=True, exist_ok=True)
        env = self._write_config(run_cwd)
        self._port = self._free_port()
        self._base_url = f"http://127.0.0.1:{self._port}"
        self._proc = await asyncio.create_subprocess_exec(
            OPENCODE_BIN, "serve", "--port", str(self._port), "--hostname", "127.0.0.1",
            env=env, cwd=str(run_cwd),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, limit=_STREAM_LIMIT)
        await self._wait_ready()
        # resume sessione OpenCode se la chat è ripresa da disco, altrimenti creane una
        if self._ocsession_file.is_file():
            self._oc_session = self._ocsession_file.read_text().strip() or None
        if not self._oc_session:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(f"{self._base_url}/session", json={})
                self._oc_session = (r.json() or {}).get("id")
            if self._oc_session:
                try:
                    self._ocsession_file.write_text(self._oc_session)
                except OSError:
                    pass
        self._client = object()   # marca avviata
        await self._set_status(ClodiaStatus.IDLE)
        intro = KIND_AUTO_INTRO.get(self.kind)
        if intro:
            asyncio.create_task(self._do_send_bg(intro))

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
                await bus.publish(Event(type="interrupted",
                                        payload={"chat_id": self.chat_id, "reason": "user_interrupt"},
                                        timestamp=datetime.now(timezone.utc)))
                await self._set_status(ClodiaStatus.IDLE)
                return note
            except Exception as e:
                await self._set_status(ClodiaStatus.ERROR)
                await self._record({"role": "system", "content": f"⚠ Errore opencode: {e}"})
                await self._publish_error(str(e))
                raise
            finally:
                self._current_turn_task = None

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
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as c:
                await c.post(f"{self._base_url}/session/{self._oc_session}/abort")
        except Exception:  # noqa: BLE001
            pass
        task.cancel()
        return True

    async def _run_turn(self, content: str) -> str:
        activity_log.append(self.kind, "run_started",
                            {"prompt": _snippet(content), "principal": self.principal,
                             "chat_id": self.chat_id})
        import httpx
        body = {
            "model": {"providerID": self._provider, "modelID": self._model},
            "agent": "build",
            "parts": [{"type": "text", "text": content}],
        }
        try:
            async with await self._http() as c:
                r = await c.post(f"{self._base_url}/session/{self._oc_session}/message", json=body)
                if r.status_code == 404 and "not found" in r.text.lower():
                    # La sessione OpenCode vive solo dentro il suo processo `opencode
                    # serve`: dopo un restart dell'agent-server l'id .ocsession di un
                    # processo precedente è invalido → 404. Ne creo una nuova e riprovo
                    # (perde la storia OpenCode-interna ma RISPONDE invece di fallire;
                    # il contesto arriva comunque dal prompt).
                    LOG.warning("opencode %s: sessione %s non trovata → ricreo",
                                self.kind, self._oc_session)
                    sr = await c.post(f"{self._base_url}/session", json={})
                    nid = (sr.json() or {}).get("id") if sr.status_code < 400 else None
                    if nid:
                        self._oc_session = nid
                        try:
                            self._ocsession_file.write_text(nid)
                        except Exception:  # noqa: BLE001
                            pass
                        r = await c.post(f"{self._base_url}/session/{self._oc_session}/message", json=body)
                if r.status_code >= 400:
                    activity_log.append(self.kind, "error",
                                        {"error": f"opencode {r.status_code}", "chat_id": self.chat_id})
                    raise RuntimeError(f"opencode HTTP {r.status_code}: {r.text[:300]}")
                data = r.json() or {}
        except (httpx.ReadTimeout, httpx.TimeoutException):
            # Turno runaway: il modello non converge entro _OPENCODE_TURN_TIMEOUT.
            # Fermo la generazione lato opencode e fallisco con errore chiaro (la
            # sessione si recupera al prossimo messaggio) invece di restare appesa.
            await self._abort_oc()
            activity_log.append(self.kind, "error",
                                {"error": "opencode turn timeout", "chat_id": self.chat_id})
            raise RuntimeError(
                f"turno opencode non concluso entro {int(_OPENCODE_TURN_TIMEOUT)}s "
                f"(modello {self._model} non convergente) — turno interrotto")
        full = await self._handle_parts(data)
        activity_log.append(self.kind, "run_done",
                            {"reply": _snippet(full), "chat_id": self.chat_id,
                             "usage": self._last_usage or None,
                             "provider": agent_effective_provider(self.kind)})
        return full

    async def _handle_parts(self, data: dict) -> str:
        parts_out: list[str] = []
        for p in data.get("parts", []) or []:
            t = p.get("type")
            if t == "text" and p.get("text"):
                parts_out.append(p["text"])
                await bus.publish(Event(type="message_chunk",
                                        payload={"chat_id": self.chat_id, "role": "assistant",
                                                 "delta": p["text"]},
                                        timestamp=datetime.now(timezone.utc)))
            elif t == "reasoning" and p.get("text"):
                await bus.publish(Event(type="thinking_chunk",
                                        payload={"chat_id": self.chat_id, "delta": p["text"]},
                                        timestamp=datetime.now(timezone.utc)))
            elif t == "tool":
                st = p.get("state") or {}
                await bus.publish(Event(type="tool_use",
                                        payload={"chat_id": self.chat_id, "tool": p.get("tool") or "tool",
                                                 "input_summary": str(st.get("input"))[:200]},
                                        timestamp=datetime.now(timezone.utc)))
        info = data.get("info") or {}
        tok = info.get("tokens") or {}
        if tok:
            cache = tok.get("cache") or {}
            self._last_usage = {
                "input_tokens": int(tok.get("input", 0) or 0),
                "output_tokens": int(tok.get("output", 0) or 0),
                "cache_read_input_tokens": int((cache.get("read") if isinstance(cache, dict) else 0) or 0),
            }
            self._context_tokens = (self._last_usage["input_tokens"]
                                    + self._last_usage["cache_read_input_tokens"])
            self._total_tokens["input"] += self._last_usage["input_tokens"]
            self._total_tokens["output"] += self._last_usage["output_tokens"]
            self._total_tokens["runs"] += 1
            await bus.publish(Event(type="usage",
                                    payload={"chat_id": self.chat_id, **self._last_usage},
                                    timestamp=datetime.now(timezone.utc)))
        return "".join(parts_out)

    async def _set_status(self, status: ClodiaStatus) -> None:
        self.status = status
        await bus.publish(Event(type="status",
                                payload={"chat_id": self.chat_id, "status": status.value},
                                timestamp=datetime.now(timezone.utc)))

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
        prefix = _resolve_title_prefix(self.kind)
        default_titles = ("", "Nuova chat", f"{prefix} Nuova chat")
        if msg.get("role") == "user" and self.title in default_titles:
            content = (msg.get("content") or "").strip().splitlines()[0] if msg.get("content") else ""
            snippet = content[:60] if content else "Nuova chat"
            self.title = f"{prefix} {snippet}"
        await bus.publish(Event(type="message",
                                payload={"chat_id": self.chat_id, **entry},
                                timestamp=datetime.now(timezone.utc)))

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
            "context_tokens": getattr(self, "_context_tokens", 0),
            "total_tokens": self._total_tokens,
            "runtime": "opencode",
            **_spawn_identity(self._spawn),
        }


def _runtime_class(kind: str):
    """Classe di runtime per il kind = SDK del provider EFFETTIVO (fallback
    cross-SDK): opencode → OpenCodeChatSession, codex → CodexChatSession,
    altrimenti ChatSession (claude)."""
    sdk = agent_runtime_sdk(kind)
    if sdk == "opencode":
        return OpenCodeChatSession
    if sdk == "codex":
        return CodexChatSession
    return ChatSession


class ChatManager:
    """Multi-chat: dict {chat_id → ChatSession}. Una chat 'default' al boot."""

    def __init__(self) -> None:
        self._chats: dict[str, "ChatSession | CodexChatSession | OpenCodeChatSession"] = {}
        self._lock = asyncio.Lock()

    def list(self) -> list[ChatSession]:
        # Ordina per ultima attività decrescente
        return sorted(self._chats.values(), key=lambda c: c.last_activity, reverse=True)

    def get(self, chat_id: str) -> ChatSession:
        if chat_id not in self._chats:
            raise KeyError(chat_id)
        return self._chats[chat_id]

    def live_spawn_dirs(self) -> set:
        """Dir di lavoro (spawn) delle sessioni VIVE — per proteggerle dallo
        sweep degli spawn orfani."""
        out: set = set()
        for c in self._chats.values():
            sp = getattr(c, "_spawn", None)
            d = getattr(sp, "dir", None) if sp is not None else None
            if d:
                out.add(str(d))
            sd = getattr(c, "_spawn_dir", None)
            if sd:
                out.add(str(sd))
        return out

    async def create(self, chat_id: Optional[str] = None, kind: str = DEFAULT_KIND) -> ChatSession:
        async with self._lock:
            # Enforcement: un agent col provider scollegato non è disponibile —
            # né per chat (qui) né per job (fire_job passa di qui). Choke point unico.
            _ensure_provider_connected(kind)
            cid = chat_id or _new_chat_id()
            if cid in self._chats:
                raise ValueError(f"chat '{cid}' already exists")
            cls = _runtime_class(kind)
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

    async def reap_idle(self, ttl_seconds, protect=None):
        """Evince le sessioni idle da più di ``ttl_seconds`` e non in mezzo a un
        turno. ``stop()`` chiude il client (libera il subprocess claude/codex) e
        fa la nice-termination dello spawn (memory symlink preservata, copia
        effimera rimossa) → recupera RAM e disco. La history resta persistita:
        ``create()`` rimaterializza la chat alla prossima apertura. NON emette
        ``chat_deleted`` — la chat non sparisce, va solo "a freddo".

        Senza questo reaping le sessioni lasciate aperte tengono vivo il loro
        subprocess (~200 MB) e lo spawn su disco a tempo indefinito, fino a
        esaurire la memoria della macchina. Ritorna gli id delle sessioni evinte.
        """
        now = datetime.now(timezone.utc)
        protect = set(protect) if protect else set()
        victims = []
        async with self._lock:
            for cid, chat in list(self._chats.items()):
                if cid in protect:
                    continue
                turn = getattr(chat, "_current_turn_task", None)
                if turn is not None and not turn.done():
                    continue  # turno in corso: non toccare
                if (now - chat.last_activity).total_seconds() < ttl_seconds:
                    continue
                self._chats.pop(cid, None)
                victims.append((cid, chat))
        reaped: list[str] = []
        for cid, chat in victims:
            try:
                await chat.stop()
                reaped.append(cid)
                await bus.publish(Event(
                    type="chat_updated",
                    payload=chat.to_dict(),
                    timestamp=datetime.now(timezone.utc),
                ))
            except Exception:  # noqa: BLE001
                LOG.warning("reap_idle: stop fallito per %s", cid, exc_info=True)
        if reaped:
            LOG.info("reap_idle: evinte %d sessioni idle (>%.0fs): %s",
                     len(reaped), ttl_seconds, ", ".join(reaped))
        return reaped

    async def drop_agent(self, agent: str):
        """Ferma le sessioni vive del SEED `agent` (restart mirato per SBLOCCARE un
        agente col runtime impuntato, es. opencode appeso su un ReadTimeout). A
        differenza di drop_all NON salta i turni in corso — anzi li **cancella**:
        una sessione bloccata ha proprio un turno appeso, ed è ciò che va ucciso.
        La history persiste → al prossimo messaggio la chat rimaterializza il seed.
        Ritorna gli id fermati."""
        seed = re.sub(r"-\d+$", "", str(agent or "").strip())
        async with self._lock:
            victims = []
            for cid, chat in list(self._chats.items()):
                if re.sub(r"-\d+$", "", str(getattr(chat, "kind", ""))) != seed:
                    continue
                self._chats.pop(cid, None)
                victims.append((cid, chat))
        stopped: list[str] = []
        for cid, chat in victims:
            # cancella un eventuale turno appeso (non troncare = irrilevante qui:
            # è wedged). Best-effort abort lato runtime + cancel del task asyncio.
            try:
                interrupt = getattr(chat, "interrupt_current_turn", None)
                if interrupt is not None:
                    await interrupt()
                else:
                    turn = getattr(chat, "_current_turn_task", None)
                    if turn is not None and not turn.done():
                        turn.cancel()
            except Exception:  # noqa: BLE001
                LOG.warning("drop_agent: interrupt fallito per %s", cid, exc_info=True)
            try:
                await chat.stop()
                stopped.append(cid)
                await bus.publish(Event(type="chat_updated", payload=chat.to_dict(),
                                        timestamp=datetime.now(timezone.utc)))
            except Exception:  # noqa: BLE001
                LOG.warning("drop_agent: stop fallito per %s", cid, exc_info=True)
        LOG.info("drop_agent(%s): fermate %d sessioni: %s", agent, len(stopped),
                 ", ".join(stopped) or "-")
        return stopped

    async def drop_all(self):
        """Ferma TUTTE le sessioni vive (restart di tutti gli agenti). La history
        persiste su disco → al prossimo messaggio la chat rimaterializza il seed
        AGGIORNATO. Salta le sessioni con un turno in corso (non tronca una
        risposta a metà). Usato dopo un update di pack che cambia seed/skill/mcp.
        Ritorna gli id fermati."""
        async with self._lock:
            victims = []
            for cid, chat in list(self._chats.items()):
                turn = getattr(chat, "_current_turn_task", None)
                if turn is not None and not turn.done():
                    continue  # turno in corso: non toccare
                self._chats.pop(cid, None)
                victims.append((cid, chat))
        stopped: list[str] = []
        for cid, chat in victims:
            try:
                await chat.stop()
                stopped.append(cid)
                await bus.publish(Event(type="chat_updated", payload=chat.to_dict(),
                                        timestamp=datetime.now(timezone.utc)))
            except Exception:  # noqa: BLE001
                LOG.warning("drop_all: stop fallito per %s", cid, exc_info=True)
        LOG.info("drop_all: fermate %d sessioni (restart agenti): %s",
                 len(stopped), ", ".join(stopped) or "-")
        return stopped


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
