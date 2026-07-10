"""Profilo d'istanza (Modular Distro, F1) — `CLODIA_DATA/profile.yaml`.

Il profilo è il contratto di runtime di un'EDIZIONE: dichiara quali feature
della piattaforma sono attive su questa istanza. È un file della datadir (non
codice): si cambia senza rebuild, con restart.

Regole fondanti (spec topic clodia-modular-distro v0.2):
- **File assente = profilo FULL**: tutte le feature attive → zero regressioni
  sulle istanze esistenti.
- File presente ma invalido → fallback FULL con warning PROMINENTE nei log
  (availability-first; il rischio "superficie riesposta" è documentato come
  rischio della spec, mitigato dal warning).
- Il backend è la fonte di verità: la webui legge `GET /profile` e non decide
  nulla da sola.
- Feature spenta = router non montato (endpoint inesistente, 404) e loop di
  background non avviato — riduzione reale della superficie.

Il gateway (clodia-tools) legge LO STESSO file per le feature che vivono lì
(`rag`, `integrations`, enforcement `topics: single`).
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import data_path

LOG = logging.getLogger("agent-server.instance_profile")

PROFILE_FILENAME = "profile.yaml"


class Features(BaseModel):
    # extra="ignore": una chiave feature scritta da un builder più nuovo non
    # deve invalidare il profilo (→ fallback FULL = superficie riaperta).
    # Stessa lezione del gateway (6 lug, tools 0.75.1), specchiata.
    model_config = ConfigDict(extra="ignore")

    jobs: bool = True
    topics: Literal["off", "single", "full"] = "full"
    # rag/integrations vivono nel gateway: qui solo dichiarate ed esposte
    # via GET /profile (la webui gata le pagine, il gateway gata i verbi).
    rag: Literal["off", "single", "full"] = "full"
    integrations: Literal["off", "fixed", "full"] = "full"
    channels: bool = True          # channel adapter Telegram (NON la webchat)
    packs_ui: bool = True
    providers_ui: bool = True
    activity: bool = True
    # Sezione/pairing PWA (Settings): spenta nelle edizioni senza PWA (§4b.6).
    pwa: bool = True
    # Popup helpdesk della webui (coda Sprint 3): non sempre necessario.
    helpdesk: bool = True
    # Motore workflow dichiarativi (pack): board /workflows + API + engine.
    # `kanban` (legacy, era il mirror Trello) resta accettato come ALIAS
    # deprecato → mappa su workflows.
    workflows: bool = False
    colony: bool = False

    @model_validator(mode="before")
    @classmethod
    def _alias_kanban(cls, data):
        if isinstance(data, dict) and "kanban" in data and "workflows" not in data:
            data = {**data, "workflows": data["kanban"]}
        return data

    @field_validator("topics", "rag", "integrations", mode="before")
    @classmethod
    def _yaml_bool_to_tristate(cls, v):
        # Gotcha YAML 1.1: `off` non quotato = booleano False (e `on` = True).
        if isinstance(v, bool):
            return "full" if v else "off"
        return v


class Branding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "Clodia Agency"
    logo: str = ""                 # path relativo alla datadir (opzionale)
    accent: str = ""               # colore CSS (opzionale)


class RagConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection: str = ""           # collection unica quando rag: single


class IntegrationsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: list[str] = Field(default_factory=list)  # whitelist per mode fixed
    # In mode fixed: l'admin può comunque montare MCP con paste manuale dalla
    # UI (decisione di terraformazione, spec v0.3 §4b.4).
    allow_manual_mcp: bool = False
    # Connettori NATIVI dell'edizione (gmail, mailboxes, trello, …).
    # None = tutti (storico); lista = solo quelli (gap-1 acme-min, 6 lug).
    connectors: Optional[list[str]] = None


class HelpdeskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str = "wainston"        # agente del popup (default wainston)


class PackOpsConfig(BaseModel):
    """Sysadmin di piattaforma (pack ops): riconcilia requires:/datastores:
    dichiarati dai pack. La piattaforma cerca il RUOLO, non il nome: le
    edizioni possono rinominare l'agente puntando qui il proprio seed.
    Se l'agente non è nel roster → degradazione pulita (nessun trigger,
    i gap restano nel report post-install)."""
    model_config = ConfigDict(extra="forbid")

    agent: str = "saimon"          # agente sysadmin (default saimon)


class TopicsSingleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "workspace"
    tier: str = "SEAL-1"


class InstanceProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    edition: str = "full"
    features: Features = Field(default_factory=Features)
    branding: Branding = Field(default_factory=Branding)
    rag: RagConfig = Field(default_factory=RagConfig)
    integrations: IntegrationsConfig = Field(default_factory=IntegrationsConfig)
    topics_single: TopicsSingleConfig = Field(default_factory=TopicsSingleConfig)
    helpdesk: HelpdeskConfig = Field(default_factory=HelpdeskConfig)
    pack_ops: PackOpsConfig = Field(default_factory=PackOpsConfig)
    # Vocabolario dell'edizione (white-label COSMETICO: UI e conversazioni
    # agentiche; API/verbi/storage restano canonici). Chiave = termine
    # canonico, valore = stringa o {singolare, plurale}.
    # Es: {topic: {singolare: pratica, plurale: pratiche}}
    vocabulary: dict = Field(default_factory=dict)
    # Default dei topic appena creati (enforcement nel gateway):
    # {participants: [clodia, ...]}.
    topics_defaults: dict = Field(default_factory=dict)
    # Pack esterni di skill da installare al boot (spec v0.3 §4b.2):
    # None/assente = tutti (comportamento storico full); lista = solo quelli
    # (anche vuota: nessun pack esterno, solo base-pack).
    skill_packs: Optional[list[str]] = None
    # Provider dell'edizione (§4b.5): None = tutto il catalogo (storico);
    # lista = /api/providers mostra solo questi e il deposito key degli altri
    # è rifiutato.
    providers: Optional[list[str]] = None


_CACHE: Optional[InstanceProfile] = None


def load(force: bool = False) -> InstanceProfile:
    """Profilo dell'istanza (cache di modulo; `force=True` per rileggere)."""
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    path = data_path(PROFILE_FILENAME)
    if not path.is_file():
        _CACHE = InstanceProfile()   # full
        return _CACHE
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("profile.yaml deve essere un mapping")
        _CACHE = InstanceProfile.model_validate(raw)
        LOG.info("profilo istanza '%s' caricato da %s", _CACHE.edition, path)
    except Exception as e:  # noqa: BLE001
        LOG.error(
            "⚠️  profile.yaml INVALIDO (%s): fallback al profilo FULL — "
            "tutte le feature attive. Correggere il file e riavviare.", e)
        _CACHE = InstanceProfile()
    return _CACHE


def vocabulary_prompt_section() -> str:
    """Sezione 'vocabolario' da appendere al system-prompt degli agenti
    (edizioni con vocabulary): l'agente parla la lingua del cliente, i verbi
    tool restano canonici."""
    vocab = load().vocabulary
    if not vocab:
        return ""
    lines = ["## Vocabolario dell'edizione",
             "",
             "Con l'utente usa SEMPRE questi termini (i nomi dei tool restano invariati):"]
    for canon, val in vocab.items():
        if isinstance(val, dict):
            sing = val.get("singolare") or canon
            plur = val.get("plurale") or sing
            lines.append(f"- «{canon}» → di' **{sing}** (plurale: {plur})")
        else:
            lines.append(f"- «{canon}» → di' **{val}**")
    return "\n".join(lines) + "\n"


def public_view() -> dict:
    """Vista per `GET /profile` (webui): features risolte + branding.

    Nessun segreto per costruzione (il profilo non ne contiene)."""
    p = load()
    return {
        "edition": p.edition,
        "features": p.features.model_dump(),
        "branding": p.branding.model_dump(),
        "rag": {"collection": p.rag.collection} if p.features.rag == "single" else {},
        "helpdesk": {"agent": p.helpdesk.agent},
        "pack_ops": {"agent": p.pack_ops.agent},
        "vocabulary": p.vocabulary,
        "topics_defaults": p.topics_defaults,
        "integrations": {
            "allow_manual_mcp": p.integrations.allow_manual_mcp,
            "connectors": p.integrations.connectors,
        },
        "topics_single": (
            p.topics_single.model_dump() if p.features.topics == "single" else {}
        ),
    }
