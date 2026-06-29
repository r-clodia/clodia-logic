"""Workspace effimero per attivazione agente.

Quando un agente viene attivato (da una card Trello o da una chat diretta),
il lane consumer / runner crea un workspace effimero copiando la definizione
Clodia-neutral (`.agent/skills`, `.agent/rules`, `system-prompt.md`) e poi
materializza il layout richiesto dal runtime agentico (`.claude/*`,
`AGENTS.md`, ecc.).

A fine task il workspace viene rimosso (cleanup automatico). La memory
sopravvive perché era un symlink.
"""
from __future__ import annotations
import json
import logging
import os
import shutil
import uuid
import fcntl
from pathlib import Path
from typing import Optional

from ..config import data_path
from .models import AgentSpec

LOG = logging.getLogger("agent-server.agents.workspace")

# Spawn degli agent (modello "/spawns" proc-like): ogni spawn è una cartella
# effimera <name>-<n> sotto clodia-data/spawns/ che materializza il seed+stato
# dell'agent + uno scratch di lavoro. Vivono sotto la datadir per sopravvivere ai
# restart del container. `SPAWNS_ROOT` è il nome nuovo; `WORKSPACES_ROOT` resta
# come alias per backcompat (test/colonia).
SPAWNS_ROOT = data_path("spawns")
WORKSPACES_ROOT = SPAWNS_ROOT
SPAWN_SERIALS_ROOT = data_path("agent-state") / "spawn-serials"

# Shared dir persistente tra agent (handoff di artefatti card-to-card).
# Mountata in ogni workspace come symlink `scratch/shared/`. L'agent scrive
# lì come fosse locale; il file fisico sopravvive alla cleanup del workspace
# e viene letto dall'agent successivo che claima la stessa card.
AGENCY_SHARED_ROOT = data_path("agency-shared")
AGENCY_SHARED_CARDS = AGENCY_SHARED_ROOT / "cards"


def _max_live_spawn_index(name: str) -> int:
    """Massimo indice visibile tra gli spawn ancora materializzati."""
    if not WORKSPACES_ROOT.is_dir():
        return 0
    mx = 0
    prefix = f"{name}-"
    for d in WORKSPACES_ROOT.iterdir():
        suffix = d.name[len(prefix):] if d.name.startswith(prefix) else ""
        if d.is_dir() and suffix.isdigit():
            mx = max(mx, int(suffix))
    return mx


def _read_spawn_serial(path: Path) -> int:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    if not raw:
        return 0
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return int(raw) if raw.isdigit() else 0
    if isinstance(data, dict):
        return int(data.get("last", 0) or 0)
    if isinstance(data, int):
        return data
    return 0


def _write_spawn_serial(path: Path, serial: int) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last": serial}) + "\n", encoding="utf-8")
    tmp.replace(path)


def _next_spawn_index(name: str) -> int:
    """Prossimo seriale persistente per gli spawn di `name`.

    Il vecchio modello guardava solo le directory vive (`name-1`, `name-2`, ...),
    quindi dopo il cleanup poteva riusare un numero. Il registro sotto
    `agent-state/spawn-serials/` rende il seriale monotono per agent seed anche
    tra cleanup e restart.
    """
    SPAWN_SERIALS_ROOT.mkdir(parents=True, exist_ok=True)
    serial_path = SPAWN_SERIALS_ROOT / f"{name}.json"
    lock_path = SPAWN_SERIALS_ROOT / f"{name}.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        last = max(_read_spawn_serial(serial_path), _max_live_spawn_index(name))
        serial = last + 1
        _write_spawn_serial(serial_path, serial)
        return serial


def _resolve_path(path_template: str, scratch_dir: Path) -> str:
    """Sostituisce `{scratch}` con il path effettivo dello scratch."""
    return path_template.replace("{scratch}", str(scratch_dir))


def _build_settings_json(spec: AgentSpec, scratch_dir: Path) -> dict:
    """Costruisce il `.claude/settings.json` per il workspace effimero.

    permissions.deny ha precedenza su permissions.allow (default Claude Code).
    `Read(...)` e `Write(...)` accettano glob.
    """
    sb = spec.sandbox
    allow: list[str] = []
    deny: list[str] = []

    for p in list(sb.allow_read):
        allow.append(f"Read({_resolve_path(p, scratch_dir)})")
    for p in list(sb.allow_write):
        allow.append(f"Write({_resolve_path(p, scratch_dir)})")
        allow.append(f"Edit({_resolve_path(p, scratch_dir)})")
    for c in sb.allow_shell_cmds:
        # Sintassi Bash documentata: 'Bash(<cmd> *)' con spazio, non ':' .
        allow.append(f"Bash({c} *)")

    for p in sb.deny_read:
        deny.append(f"Read({_resolve_path(p, scratch_dir)})")
    for pattern in sb.deny_shell_patterns:
        deny.append(f"Bash({pattern})")

    return {
        "permissions": {
            "allow": allow,
            "deny": deny,
        },
        "hooks": {},
    }


class EphemeralWorkspace:
    """Workspace effimero per una singola attivazione di un agente."""

    def __init__(
        self,
        spec: AgentSpec,
        task_id: Optional[str] = None,
        shared_subdir: Optional[str] = None,
        execution_id: Optional[str] = None,
    ) -> None:
        self.spec = spec
        # Senza task_id esplicito (es. webchat) → indice sequenziale proc-like
        # (name-1, name-2, …). I chiamanti colonia passano un task_id proprio.
        self.task_id = task_id or str(_next_spawn_index(spec.name))
        self.execution_id = execution_id  # legacy param (colony), non più usato
        self.dir = WORKSPACES_ROOT / f"{spec.name}-{self.task_id}"
        self.scratch = self.dir / "scratch"
        # Se fornito (es. card_id dal skill_consumer), in `scratch/shared/`
        # viene creato un symlink alla dir persistente per quella card. Gli
        # agent che lavorano sequenzialmente la stessa card si scambiano
        # artefatti tramite quella dir senza dover allegarli a Trello.
        self.shared_subdir = shared_subdir

    def create(self) -> Path:
        """Materializza il workspace su disco. Ritorna il path creato."""
        if self.spec.agent_dir is None:
            raise RuntimeError(f"agent '{self.spec.name}': agent_dir non risolto")
        agent_dir = Path(self.spec.agent_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(parents=True, exist_ok=True)

        # .agent/skills/ — copia neutral delle skill custom dell'agente.
        # I runtime adapter potranno poi convertirle nel naming/layout che il
        # rispettivo CLI agentico si aspetta.
        agent_root = self.dir / ".agent"
        skills_target = agent_root / "skills"
        skills_target.mkdir(parents=True, exist_ok=True)
        for rel in self.spec.skills:
            src = agent_dir / rel
            if not src.is_file():
                LOG.warning("Skill %s non trovata (%s), skip", rel, src)
                continue
            shutil.copy2(src, skills_target / src.name)

        # Auto-sync delle capability dal catalog centralizzato. Per ogni
        # voce in spec.capabilities copia ricorsivamente la skill folder
        # da skills-catalog/<name>/ in .agent/skills/<name>/. Vedi
        # skills-catalog/README.md per il modello "single source + copy".
        from . import skill_sync
        # Eredità di specie: un agent riceve dai suoi `parents` (1-2 ancestor) le
        # loro skill come attributi innati. Capabilities effettive = proprie +
        # union delle capabilities dei parents (un livello). La wildcard "*" è
        # gestita a valle da materialize_capabilities.
        caps = list(self.spec.capabilities)
        if self.spec.parents:
            from .loader import registry as _registry
            for pname in self.spec.parents:
                anc = _registry.get_by_name(pname)
                if anc is not None:
                    caps.extend(anc.capabilities)
                else:
                    LOG.warning("ancestor '%s' di %s non risolto nel registry", pname, self.spec.name)
        caps = list(dict.fromkeys(caps))  # dedup, ordine preservato
        copied, unresolved = skill_sync.materialize_capabilities(caps, skills_target)
        if copied or unresolved:
            LOG.info(
                "skill_sync agent=%s: %d copiate, unresolved=%s",
                self.spec.name, copied, unresolved,
            )

        # Auto-sync delle rules dal catalog (dual). Per ogni voce in
        # spec.rules copia il file <name>.md da rules-catalog/ (data o
        # logic) in .agent/rules/<name>.md. Il runtime adapter decide poi
        # come esporle al suo agente.
        from . import rule_sync
        rules_target = agent_root / "rules"
        r_copied, r_unresolved = rule_sync.materialize_rules(
            self.spec.rules, rules_target
        )
        if r_copied or r_unresolved:
            LOG.info(
                "rule_sync agent=%s: %d copiate, unresolved=%s",
                self.spec.name, r_copied, r_unresolved,
            )

        # memory: symlink se configurata, altrimenti niente
        if self.spec.memory is not None:
            mem_src = agent_dir / self.spec.memory.dir
            if mem_src.is_dir():
                mem_link = agent_root / "memory"
                # Symlink relativo per portabilità tra container e host
                if mem_link.exists() or mem_link.is_symlink():
                    mem_link.unlink()
                os.symlink(str(mem_src.resolve()), str(mem_link))

        # system-prompt.md = costituzione (genoma, se referenziata) FUSA in testa
        # + system prompt dell'agent. Unico punto di fusione: entrambi i motori
        # consumano system-prompt.md (claude come system_prompt; codex lo legge
        # via AGENTS.md). La costituzione è componente innata del seed.
        from .constitution_sync import load_constitution_text
        constitution = load_constitution_text(self.spec.constitution)
        sp_src = agent_dir / self.spec.system_prompt
        prompt_body = ""
        if sp_src.is_file():
            prompt_body = sp_src.read_text(encoding="utf-8")
        else:
            LOG.warning("system_prompt %s non trovato per agent %s", sp_src, self.spec.name)
        parts = [p.strip() for p in (constitution, prompt_body) if p and p.strip()]
        fused = ("\n\n---\n\n".join(parts) + "\n") if parts else ""
        (self.dir / "system-prompt.md").write_text(fused, encoding="utf-8")

        # scratch/shared/ → symlink alla agency-shared dir per la card (se
        # shared_subdir fornito). Persistente tra spawn di agent diversi.
        if self.shared_subdir:
            shared_real = AGENCY_SHARED_CARDS / self.shared_subdir
            shared_real.mkdir(parents=True, exist_ok=True)
            shared_link = self.scratch / "shared"
            if shared_link.exists() or shared_link.is_symlink():
                try:
                    shared_link.unlink()
                except OSError:
                    pass
            os.symlink(str(shared_real.resolve()), str(shared_link))

        self._materialize_runtime_layout(agent_root)

        LOG.info(
            "Workspace effimero creato: %s (agent=%s, task=%s)",
            self.dir, self.spec.name, self.task_id,
        )
        return self.dir

    def cleanup(self) -> None:
        """Rimuove il workspace effimero. La memory (symlink) resta intatta."""
        if not self.dir.is_dir():
            return
        # Rimuovi il symlink memory prima del rmtree per evitare di cancellare
        # i file reali sotto agents/<name>/memory/.
        mem_link = self.dir / ".agent" / "memory"
        if mem_link.is_symlink():
            mem_link.unlink()
        shutil.rmtree(self.dir, ignore_errors=True)
        LOG.info("Workspace effimero rimosso: %s", self.dir)

    def __enter__(self) -> "EphemeralWorkspace":
        self.create()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.cleanup()

    def _materialize_runtime_layout(self, agent_root: Path) -> None:
        if self.spec.agent_sdk == "claude":
            self._materialize_claude_layout(agent_root)
        elif self.spec.agent_sdk == "codex":
            self._materialize_codex_layout(agent_root)
        elif self.spec.agent_sdk == "opencode":
            self._materialize_opencode_layout(agent_root)
        else:  # pragma: no cover - AgentSpec valida i valori ammessi
            raise RuntimeError(f"agent_sdk non supportato: {self.spec.agent_sdk}")

    def _materialize_claude_layout(self, agent_root: Path) -> None:
        claude_root = self.dir / ".claude"
        claude_root.mkdir(parents=True, exist_ok=True)
        skills_src = agent_root / "skills"
        rules_src = agent_root / "rules"
        if skills_src.is_dir():
            shutil.copytree(skills_src, claude_root / "skills", dirs_exist_ok=True)
        if rules_src.is_dir():
            shutil.copytree(rules_src, claude_root / "rules", dirs_exist_ok=True)
        mem_src = agent_root / "memory"
        if mem_src.is_symlink():
            mem_claude_link = claude_root / "memory"
            if mem_claude_link.exists() or mem_claude_link.is_symlink():
                mem_claude_link.unlink()
            os.symlink(str(mem_src.resolve()), str(mem_claude_link))

        settings = _build_settings_json(self.spec, self.scratch)
        settings_path = claude_root / "settings.local.json"
        settings_path.write_text(json.dumps(settings, indent=2))

    def _codex_agents_md(self, agent_root: Path) -> str:
        """AGENTS.md per codex: costituzione + identità (dal system-prompt.md
        materializzato, che codex auto-carica) in testa + runtime context
        (skill/rules/handoff). Così l'agent codex è governato dal seed."""
        parts: list[str] = []
        sp = self.dir / "system-prompt.md"
        if sp.is_file():
            body = sp.read_text(encoding="utf-8").strip()
            if body:
                parts.append(body)
        parts.append(_build_codex_agents_md(self.spec, agent_root))
        return "\n\n---\n\n".join(parts) + "\n"

    def _materialize_codex_layout(self, agent_root: Path) -> None:
        (self.dir / "AGENTS.md").write_text(self._codex_agents_md(agent_root))

    def _materialize_opencode_layout(self, agent_root: Path) -> None:
        # Placeholder esplicito per il prossimo runtime: la definizione resta
        # agnostica, ma senza adapter operativo non avviamo task opencode.
        (self.dir / "AGENTS.md").write_text(self._codex_agents_md(agent_root))


def _rel_paths(root: Path, pattern: str) -> list[str]:
    if not root.is_dir():
        return []
    out: list[str] = []
    for path in sorted(root.glob(pattern)):
        if path.is_file():
            out.append(str(path.relative_to(root.parent.parent)))
    return out


def _build_codex_agents_md(spec: AgentSpec, agent_root: Path) -> str:
    skill_root = agent_root / "skills"
    skills = _rel_paths(skill_root, "**/SKILL.md") + _rel_paths(skill_root, "*.md")
    rules = _rel_paths(agent_root / "rules", "*.md")
    skill_lines = "\n".join(f"- `{p}`" for p in skills) or "- (nessuna skill materializzata)"
    rule_lines = "\n".join(f"- `{p}`" for p in rules) or "- (nessuna rule materializzata)"
    capabilities = ", ".join(spec.capabilities) if spec.capabilities else "(nessuna)"
    return f"""# Agent Runtime Context

Sei `{spec.name}` (`{spec.display_name}`), un agente Clodia eseguito tramite `{spec.agent_sdk}`.

## Istruzioni obbligatorie

- Leggi `system-prompt.md` prima di lavorare.
- Lavora solo nel workspace effimero e nello scratch indicato.
- Non leggere `secrets/**` e non stampare credenziali.
- Se devi consegnare un risultato al dispatcher inbox, scrivi `scratch/handoff.json`.
- Le skill descrivono il dominio del lavoro. Non contaminare l'output della
  skill con dettagli di routing/control-plane se il task non lo richiede.
- Se stai lavorando una card skill-driven, leggi la skill richiesta nel prompt
  iniziale per il lavoro di dominio.

## Capability dichiarate

{capabilities}

## Skill disponibili

{skill_lines}

## Rules disponibili

{rule_lines}

## Contratto minimo handoff

```json
{{
  "comment": "agent: {spec.name}\\n\\n<sintesi>",
  "attachments": [],
  "pass_to": "owner"
}}
```
"""
