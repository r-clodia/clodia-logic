"""Auto-provisioning del responder confinato di un canale (clone per-topic).

Per ogni topic-con-channel si clona dall'archetipo `catalogs/agent-templates/eco`
un'identità confinata `eco-<topic>`, con `clearance = tier del topic`, partecipe
SOLO di quel topic. Combinata con l'ACL participants del gateway
(project_topic_access_two_axis), dà il confinamento per-topic reale: anche se in
futuro il responder avrà tool `topic.*`, potrà toccare solo il suo topic.

NB: Eco (responder-nel-canale, senza portata esterna) è distinto da Messaggero
(agents-seed/messaggero), l'agente messaggero con i tool email/telegram.
"""
from __future__ import annotations

import logging
import shutil

import yaml

from ..config import WORKSPACE_ROOT
from ..agents import registry
from ..agents.loader import AGENTS_DIR
from . import gateway_admin, topics_client

LOG = logging.getLogger("agent-server.channel_responder")

_ARCHETYPE = WORKSPACE_ROOT / "catalogs" / "agent-templates" / "eco"


def responder_name(topic_name: str) -> str:
    return f"eco-{topic_name}"


def _clone(rn: str, clearance: str) -> None:
    dst = AGENTS_DIR / rn
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "memory").mkdir(exist_ok=True)
    spec = yaml.safe_load((_ARCHETYPE / "agent.yaml").read_text(encoding="utf-8"))
    spec["name"] = rn
    spec["display_name"] = "Eco"
    spec["clearance"] = clearance  # = tier del topic → clearance ≥ tier per costruzione
    (dst / "agent.yaml").write_text(
        yaml.safe_dump(spec, sort_keys=False, allow_unicode=True), encoding="utf-8")
    shutil.copy(_ARCHETYPE / "system-prompt.md", dst / "system-prompt.md")
    LOG.info("clonato responder confinato %s (clearance=%s)", rn, clearance)


def ensure_responder(tier: str, name: str, topic_tier: str) -> str:
    """Idempotente: garantisce che esista l'identità confinata del canale e che
    sia participant del topic. Ritorna il nome del responder."""
    rn = responder_name(name)
    if registry.get_by_name(rn) is None:
        if not (_ARCHETYPE / "agent.yaml").is_file():
            LOG.warning("archetipo eco assente in %s", _ARCHETYPE)
            return rn
        _clone(rn, topic_tier)
        registry.load()
    # Registrazione nel gateway SEMPRE (idempotente): il config.yaml del gateway
    # è baked nell'immagine → un rebuild azzera le registrazioni runtime. Ripeterla
    # a ogni tick rende il clone auto-guarente (torna dopo un rebuild del gateway).
    try:
        gateway_admin.register_agent(rn, allowed_tools=[])
    except Exception as e:  # noqa: BLE001
        LOG.warning("register_agent %s nel gateway fallita: %s", rn, e)
    try:
        topics_client.set_participant(tier, name, rn, add=True)
    except Exception as e:  # noqa: BLE001
        LOG.warning("set_participant %s su %s/%s: %s", rn, tier, name, e)
    return rn
