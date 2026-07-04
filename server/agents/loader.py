"""Discovery + parsing degli agent.yaml in `clodia-data/agents/`.

La directory `agents/` vive sotto la datadir (`CLODIA_DATA/agents`), così
gli agenti persistono indipendentemente dai rebuild dell'immagine.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Iterator, Optional

import yaml
from pydantic import ValidationError

from ..config import data_path
from .models import AgentSpec

LOG = logging.getLogger("agent-server.agents")

AGENTS_DIR = data_path("agents")


class AgentRegistry:
    """Cache in memoria degli agenti definiti. Ricaricabile a runtime
    (utile in dev: edit dell'agent.yaml + POST /api/agents/reload).
    """

    def __init__(self, base_dir: Path = AGENTS_DIR) -> None:
        self.base_dir = base_dir
        self._agents: dict[str, AgentSpec] = {}
        self._errors: dict[str, str] = {}

    def discover(self) -> Iterator[Path]:
        """Yields i path agent.yaml trovati sotto base_dir."""
        if not self.base_dir.is_dir():
            return
        for child in sorted(self.base_dir.iterdir()):
            if not child.is_dir():
                continue
            spec_file = child / "agent.yaml"
            if spec_file.is_file():
                yield spec_file

    def load(self) -> None:
        """Ricarica tutta la registry dal filesystem."""
        self._agents.clear()
        self._errors.clear()
        for spec_file in self.discover():
            agent_dir = spec_file.parent
            try:
                with spec_file.open() as f:
                    raw = yaml.safe_load(f) or {}
                spec = AgentSpec.model_validate(raw)
                spec.agent_dir = str(agent_dir)
                if spec.name != agent_dir.name:
                    raise ValueError(
                        f"agent.name '{spec.name}' non corrisponde alla cartella "
                        f"'{agent_dir.name}' — devono coincidere"
                    )
                # Description derivata dinamicamente da system prompt +
                # capabilities + rules. Sovrascrive la description statica
                # nell'agent.yaml (che resta come seed/fallback).
                derived = _derive_description(spec, agent_dir)
                if derived:
                    spec.description = derived
                if spec.skills:
                    LOG.warning(
                        "agent '%s': campo `skills` DEPRECATO (AgentSpec v2) — "
                        "migrare i file a una skill del data catalog e usare "
                        "`capabilities`", spec.name)
                if spec.can_delegate_to:
                    LOG.warning(
                        "agent '%s': campo `can_delegate_to` DEPRECATO "
                        "(AgentSpec v2) — la delega è il movimento di card",
                        spec.name)
                self._agents[spec.name] = spec
                LOG.info("Caricato agent '%s' da %s", spec.name, spec_file)
            except (ValidationError, ValueError, yaml.YAMLError) as e:
                self._errors[agent_dir.name] = str(e)
                LOG.warning("Errore parsing agent '%s': %s", agent_dir.name, e)

    def get(self, name: str) -> AgentSpec:
        return self._agents[name]

    def list(self) -> list[AgentSpec]:
        return list(self._agents.values())

    def errors(self) -> dict[str, str]:
        return dict(self._errors)

    def get_by_name(self, name: str) -> Optional[AgentSpec]:
        return self._agents.get(name)

    def get_by_telegram(self, handle: Optional[str]) -> Optional[AgentSpec]:
        """Lookup inverso `handle/chat_id Telegram → principal HUMAN registrato`.

        Ritorna lo spec dell'human con `telegram` corrispondente, o None (→ il
        mittente è un proxy, non registrato). Match tollerante: ignora un '@'
        iniziale, gli spazi e il case. Solo `type == "human"`: gli AI non sono
        committenti-umani di un canale."""
        if handle is None:
            return None
        want = str(handle).lstrip("@").strip().lower()
        if not want:
            return None
        for spec in self._agents.values():
            if spec.type != "human" or not spec.telegram:
                continue
            if str(spec.telegram).lstrip("@").strip().lower() == want:
                return spec
        return None


# Singleton globale; caricata al primo accesso, ricaricabile via API.
registry = AgentRegistry()


# ── description derivation ─────────────────────────────────────────


def _first_sentence(text: str, max_len: int = 220) -> str:
    """Prima frase di senso compiuto da `text`: salta righe vuote, header
    markdown, blocchi frontmatter `---`. Ritorna massimo `max_len` char,
    troncando alla fine della prima frase se possibile."""
    in_frontmatter = False
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if line.startswith("#") or line.startswith(">") or line.startswith("```"):
            continue
        # Prendo fino al primo terminatore di frase, altrimenti tronco
        for sep in (". ", "! ", "? "):
            i = line.find(sep)
            if 0 < i <= max_len:
                return line[: i + 1].strip()
        return line[:max_len].rstrip() + ("…" if len(line) > max_len else "")
    return ""


def _derive_description(spec: AgentSpec, agent_dir) -> str:
    """Compone una description sintetica da: prima frase del system-prompt,
    capabilities (skip `kanban-operations` base presente su tutti), rules.
    Pattern deterministico, no LLM call."""
    parts: list[str] = []
    # I principal `human` non hanno system_prompt (non eseguiti).
    if spec.system_prompt:
        sp_path = agent_dir / spec.system_prompt
        if sp_path.is_file():
            sent = _first_sentence(sp_path.read_text())
            if sent:
                parts.append(sent)
    caps = [c for c in (spec.capabilities or []) if c != "kanban-operations"]
    if caps:
        parts.append("Skill: " + ", ".join(caps) + ".")
    if spec.rules:
        parts.append("Rules: " + ", ".join(spec.rules) + ".")
    return " ".join(parts)


registry.load()
