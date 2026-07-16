"""Topic playbooks — benvenuto con action pills nei topic/pratiche nuovi.

I plugin dichiarano `topic_playbooks: {tipo: [{label, skill?}]}` nel manifest
(curated dal pack developer, propagato dall'import in plugin.yaml). Alla
creazione di un topic la piattaforma posta un messaggio di benvenuto del
contact agent con le pills del TIPO, filtrate su ciò che gli agenti
PARTECIPANTI sanno davvero fare (capabilities → skill possedute): la pill
esiste solo se nel canale c'è chi la sa eseguire. Zero token: il benvenuto è
composto in codice, non da un turno LLM; le pills usano il markup choices
della webui (click = invio del testo).
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .. import instance_profile
from ..agents.loader import registry
from ..agents.skill_sync import WILDCARDS, _all_skill_names, _pack_skill_names
from ..config import data_path

LOG = logging.getLogger("agent-server.topic_playbooks")


def _plugin_playbooks() -> dict[str, list[dict[str, str]]]:
    """Unione dei topic_playbooks: plugin installati + profilo dell'istanza.

    Oltre ai manifest dei plugin (curated dal pack developer), anche il
    profilo può dichiarare `topics_defaults.playbooks` — per le istanze che
    vogliono pills sui propri tipi senza passare da un pack (es. personal
    con i tipi storici). Stessa forma: {tipo: [{label, skill?}]}."""
    merged: dict[str, list[dict[str, str]]] = {}
    prof_pb = (instance_profile.load().topics_defaults or {}).get("playbooks") or {}
    for ttype, pills in prof_pb.items():
        if isinstance(pills, list):
            merged.setdefault(str(ttype), []).extend(
                p for p in pills if isinstance(p, dict) and p.get("label"))
    for manifest in sorted(Path(data_path("plugins")).glob("*/plugin.yaml")):
        try:
            meta = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(meta, dict):
            continue
        for ttype, pills in (meta.get("topic_playbooks") or {}).items():
            if isinstance(pills, list):
                merged.setdefault(str(ttype), []).extend(
                    p for p in pills if isinstance(p, dict) and p.get("label"))
    return merged


def _agent_skill_names(agent: str) -> set[str]:
    """Skill possedute da un agente (capabilities espanse: wildcard e pack-glob)."""
    try:
        spec = registry.get(agent)
    except KeyError:
        return set()
    caps = list(getattr(spec, "capabilities", None) or [])
    if any(c in WILDCARDS for c in caps):
        return set(_all_skill_names())
    out: set[str] = set()
    for cap in caps:
        if cap.endswith("/*"):
            out.update(_pack_skill_names(cap[:-2]))
        else:
            out.add(cap)
    return out


def pills_for(topic_type: str, participants: list[str]) -> list[str]:
    """Label delle pills per il tipo, filtrate sulle skill dei partecipanti AI."""
    pills = _plugin_playbooks().get(topic_type or "", [])
    if not pills:
        return []
    owned: set[str] = set()
    for p in participants:
        owned |= _agent_skill_names(p)
    out: list[str] = []
    for pill in pills:
        req = pill.get("skill")
        if req and req not in owned:
            continue
        label = str(pill["label"]).replace(",", " –").strip()
        if label and label not in out:
            out.append(label)
    return out


def _coordinator_can_compose(contact_agent: str | None) -> bool:
    """True se il contact agent è un super-agent (coordinatore): in quel caso il
    benvenuto invita a descrivere il topic per comporre la squadra (skill
    team-composition). I super-agent hanno tutto il catalog → hanno la skill."""
    if not contact_agent:
        return False
    spec = registry.get_by_name(contact_agent)
    return bool(spec and getattr(spec, "type", None) == "super")


def welcome_message(name: str, title: str, topic_type: str,
                    participants: list[str],
                    contact_agent: str | None = None) -> str | None:
    """Testo del benvenuto (o None se l'edizione non lo prevede).

    Si posta se ci sono pills per il tipo, se l'edizione dichiara i tipi nel
    profilo (edizione verticale), oppure se il contact agent è un coordinatore
    (super-agent) che può comporre la squadra: in quel caso chiede di cosa
    tratta il topic per proporre gli agenti da invitare (team-composition)."""
    pills = pills_for(topic_type, participants)
    prof = instance_profile.load()
    types_conf = (prof.topics_defaults or {}).get("types") or []
    compose = _coordinator_can_compose(contact_agent)
    if not pills and not types_conf and not compose:
        return None
    vocab_topic = prof.vocabulary.get("topic")
    if isinstance(vocab_topic, dict):
        noun = vocab_topic.get("singolare") or "topic"
    else:
        noun = str(vocab_topic) if vocab_topic else "topic"
    label = topic_type or "progetto"
    for t in types_conf:
        if isinstance(t, dict) and t.get("key") == topic_type:
            label = t.get("label") or topic_type
            break
    # Genere dell'articolo: euristica it-IT sul sostantivo del vocabolario
    # (pratica → Questa/pronta; topic/canale → Questo/pronto).
    fem = noun.lower().endswith("a")
    questo, pronto = ("Questa", "pronta") if fem else ("Questo", "pronto")
    lines = [f"Ciao! {questo} {noun} — **{title or name}** ({label}) — è {pronto}."]
    if compose:
        # team-composition PRIMA di tutto: in un topic nuovo si compone la squadra,
        # poi si pianifica. Ha precedenza sulle pills di planning (che tornano
        # naturali dopo, quando gli specialisti sono a bordo).
        lines.append(
            f"Di cosa tratta {questo.lower()} {noun}? Descrivimelo in una riga e "
            "ti propongo la **squadra di agenti** più adatta da invitare — i più "
            "specializzati e meno costosi per il caso.")
    elif pills:
        lines.append("Posso partire subito con una di queste attività:")
        lines.append(f"<!-- choices={','.join(pills)} -->")
    else:
        lines.append("Descrivimi l'esigenza e imposto il lavoro.")
    return "\n\n".join(lines)
