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
_ALIAS_RE = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


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
    colony: bool = False

    @field_validator("topics", "rag", "integrations", mode="before")
    @classmethod
    def _yaml_bool_to_tristate(cls, v):
        # Gotcha YAML 1.1: `off` non quotato = booleano False (e `on` = True).
        if isinstance(v, bool):
            return "full" if v else "off"
        return v


class Branding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: VUOTO significa «nessuna opinione», non «senza nome»: l'interfaccia
    #: mostra allora il suo aspetto storico. Il default era `"Clodia Agency"`, e
    #: finché il branding valeva solo per le edizioni custom non faceva danno —
    #: nessuno lo leggeva altrove. Nel momento in cui vale ovunque sia
    #: configurato, un default non vuoto **rinominerebbe ogni istanza esistente**
    #: senza che nessuno l'abbia deciso. È la distinzione fra `None` e `[]` della
    #: specifica, sul campo più visibile che ci sia.
    name: str = ""
    #: Ragione sociale: il nome CON CUI L'AZIENDA SI FIRMA. Distinto da `name`,
    #: che è come l'istanza si chiama nell'interfaccia — «Studio Carboni» in
    #: sidebar può convivere con «Uncommon Digital Srl a socio unico» in fondo a
    #: una pagina o in un documento. Fonderli costringerebbe a scegliere fra un
    #: titolo lungo e una firma incompleta.
    legal_name: str = ""
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
    # Connettori NATIVI dell'edizione (gmail, mailboxes, …).
    # None = tutti (storico); lista = solo quelli (gap-1 acme-min, 6 lug).
    connectors: Optional[list[str]] = None


class HelpdeskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str = "sysadmin"      # agente del popup (default sysadmin: steward, ex janitor+sysadmin)


class PackOpsConfig(BaseModel):
    """Pack ops agentico.

    Default ON: il gateway espone tool dedicati e stretti per install pip/npm in
    path persistenti, verificare binari, montare MCP e provisionare RAG. Le
    edizioni che vogliono gestire il setup solo manualmente possono spegnerlo.
    """
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    agent: str = "sysadmin"          # agente sysadmin (default sysadmin)


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
    # Macro instance-wide per i messaggi dei canali: $simbolo → prompt completo.
    # Le chiavi possono essere configurate con o senza il dollaro iniziale.
    channel_aliases: dict = Field(default_factory=dict)
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
        "channel_aliases": p.channel_aliases,
        "integrations": {
            "allow_manual_mcp": p.integrations.allow_manual_mcp,
            "connectors": p.integrations.connectors,
        },
        "topics_single": (
            p.topics_single.model_dump() if p.features.topics == "single" else {}
        ),
    }


def _profile_raw() -> dict:
    path = data_path(PROFILE_FILENAME)
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("profile.yaml deve essere un mapping")
    return raw


def normalize_channel_aliases(aliases: dict) -> dict[str, str]:
    """Normalizza e valida `$alias -> prompt` per profile.yaml."""
    import re
    out: dict[str, str] = {}
    if not isinstance(aliases, dict):
        raise ValueError("channel_aliases deve essere un mapping")
    for raw_key, raw_value in aliases.items():
        key = str(raw_key or "").strip()
        if key.startswith("$"):
            key = key[1:]
        value = str(raw_value or "").strip()
        if not key:
            continue
        if not re.fullmatch(_ALIAS_RE, key):
            raise ValueError(f"alias non valido: {raw_key!r}")
        if not value:
            continue
        out[key] = value
    return dict(sorted(out.items()))


def update_channel_aliases(aliases: dict) -> dict:
    """Aggiorna channel_aliases nel profilo datadir e ricarica la cache."""
    path = data_path(PROFILE_FILENAME)
    raw = _profile_raw()
    raw["channel_aliases"] = normalize_channel_aliases(aliases)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    load(force=True)
    return public_view()
