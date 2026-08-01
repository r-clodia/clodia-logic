"""Danger score «lethal trifecta» di agenti e contesti (issue clodia-platform#77).

La lethal trifecta descrive tre capacità che, presenti INSIEME nello stesso
flusso, rendono un agente strutturalmente esposto alla prompt injection
indiretta:

1. **private_data** — accesso a dati privati;
2. **untrusted_input** — esposizione a contenuto non fidato;
3. **egress** — capacità di far uscire dati verso l'esterno.

Qui si implementa il **primo step** dell'issue: il punteggio si **calcola** dai
grant effettivi (`tool_permissions`) e dai partecipanti del canale — non si
dichiara. Nessun enforcement: questo modulo misura e spiega, non blocca.

Tre scelte di modello, tutte prese dall'issue:

- **L'unità di valutazione non è l'agente, è il contesto.** Il profilo di un
  canale è l'OR dei profili dei suoi agenti, calcolato sulla **chiusura**:
  partecipanti + agenti raggiungibili da chi può allargare la composizione
  (`topic.add_participant`, `agents.*`, `*`). Chi porta l'estensione è
  riportato in `expanded_by`, così il numero resta spiegabile.
- **La shell è un flag separato, non un quarto lato.** Un agente con bash e
  rete non rende il canale «più pericoloso»: rende il controllo **aggirabile**
  (`curl` non passa dal gateway). È una proprietà diversa e va mostrata come
  tale.
- **La classificazione dei verbi è configurazione versionata**
  (`catalogs/trifecta.yaml`), non costanti nel codice: si rifinisce per verbo
  con una PR, senza toccare la logica. Override d'istanza opzionale in
  `CLODIA_DATA/trifecta.yaml`.

I principal `human` non contribuiscono ad alcun lato: non eseguono tool. Sono
l'unico declassificatore legittimo, non una capacità del canale.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

import yaml

from ..config import data_path, workspace_path

LOG = logging.getLogger("agent-server.agents.trifecta")

#: I tre lati del triangolo, in ordine di presentazione.
LEGS = ("private_data", "untrusted_input", "egress")

#: Simboli richiesti dall'issue (#77, commento del 1 ago): 1/3 ✅ 2/3 ⚠️ 3/3 🚨.
SYMBOLS = {0: "✅", 1: "✅", 2: "⚠️", 3: "🚨"}

#: Path della classificazione versionata nel repo + override d'istanza.
_REPO_CONFIG = "catalogs/trifecta.yaml"
_DATA_CONFIG = "trifecta.yaml"

#: Quanti grant elencare come motivazione per lato (la UI mostra un tooltip,
#: non un audit: l'elenco completo resterebbe illeggibile).
_MAX_REASONS = 6

_CACHE: Optional[dict] = None


# ── classificazione ───────────────────────────────────────────────────────

def _empty_leg() -> dict:
    return {"include": [], "exclude": []}


def _empty_config() -> dict:
    return {"version": 0, "expansion": _empty_leg(),
            **{leg: _empty_leg() for leg in LEGS}}


def _parse_config(raw: dict) -> dict:
    """Una voce che inizia con `-` è un'**eccezione**: toglie dal lato i grant
    che vi ricadono interamente. Serve a tenere i namespace larghi (`email.*`,
    a prova di verbi futuri) senza che `email.send` — l'unico verbo di uscita
    del namespace — accenda anche «dati privati» e «contenuto non fidato»."""
    cfg = _empty_config()
    cfg["version"] = raw.get("version", 0)
    for key in (*LEGS, "expansion"):
        value = raw.get(key) or []
        if not isinstance(value, list):
            raise ValueError(f"'{key}' deve essere una lista di pattern")
        entry = _empty_leg()
        for item in value:
            pattern = str(item).strip()
            if not pattern:
                continue
            if pattern.startswith("-"):
                entry["exclude"].append(pattern[1:].strip())
            else:
                entry["include"].append(pattern)
        cfg[key] = entry
    return cfg


def _merge_config(base: dict, extra: dict) -> dict:
    """Fonde l'override d'istanza SOPRA il catalogo del repo, per lato e in modo
    ADDITIVO.

    Non sostituisce: un override che dichiara solo `egress` deve aggiungere i
    suoi pattern di uscita, non azzerare `private_data` e `untrusted_input`.
    Sostituendo, un agente realmente 3/3 verrebbe mostrato 0/3 — una falsa
    rassicurazione, cioè la sola direzione d'errore che questa misura non può
    permettersi. Per TOGLIERE una classificazione del repo esistono le
    eccezioni `-pattern`, che restano il meccanismo esplicito.
    """
    out = _empty_config()
    out["version"] = extra.get("version") or base.get("version", 0)
    for key in (*LEGS, "expansion"):
        for field in ("include", "exclude"):
            merged = list(base[key][field]) + [p for p in extra[key][field]
                                               if p not in base[key][field]]
            out[key][field] = merged
    return out


def load_config(force: bool = False) -> dict:
    """Classificazione dei verbi. In cache: è un file versionato, cambia con un
    deploy. `force=True` la ricarica (usato dai test e da un eventuale reload)."""
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    cfg = _empty_config()
    for path in (workspace_path(_REPO_CONFIG), data_path(_DATA_CONFIG)):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            continue
        except (OSError, yaml.YAMLError) as e:
            LOG.warning("trifecta: config %s illeggibile (%s) — ignorata", path, e)
            continue
        try:
            cfg = _merge_config(cfg, _parse_config(raw))
        except ValueError as e:
            LOG.warning("trifecta: config %s malformata (%s) — ignorata", path, e)
    if not any(cfg[leg]["include"] for leg in LEGS):
        # Nessuna classificazione = nessun punteggio calcolabile. Meglio dirlo
        # (il chiamante espone version=0) che restituire 0/3 rassicuranti.
        LOG.warning("trifecta: nessuna classificazione caricata da %s", _REPO_CONFIG)
    _CACHE = cfg
    return cfg


# ── match dei pattern ─────────────────────────────────────────────────────

def _split(pattern: str) -> tuple[str, str]:
    """`ns.verb` → (ns, verb); `ns.*`/`ns` → (ns, '*'); `*` → ('*', '*')."""
    p = (pattern or "").strip()
    if p in ("*", "**", ""):
        return "*", "*"
    if "." in p:
        ns, verb = p.split(".", 1)
        return (ns or "*"), (verb.strip() or "*")
    return p, "*"


def _overlap(a: str, b: str) -> bool:
    """True se i due pattern possono designare lo stesso verbo.

    Volutamente simmetrico: vale sia `email.*` ⊃ `email.send` (grant ampio,
    verbo classificato puntuale) sia `email.send` ⊂ `email.*` (grant puntuale,
    classificazione per namespace)."""
    a_ns, a_verb = _split(a)
    b_ns, b_verb = _split(b)
    if a_ns != "*" and b_ns != "*" and a_ns != b_ns:
        return False
    if a_verb != "*" and b_verb != "*" and a_verb != b_verb:
        return False
    return True


def _covers(pattern: str, grant: str) -> bool:
    """True se `pattern` copre INTERAMENTE `grant` (non solo l'intersezione).

    Usato dalle eccezioni: `-email.send` toglie il grant `email.send`, ma non
    tocca `email.*`, che continua a dare accesso ai verbi di lettura."""
    p_ns, p_verb = _split(pattern)
    g_ns, g_verb = _split(grant)
    if p_ns != "*" and (g_ns == "*" or p_ns != g_ns):
        return False
    if p_verb != "*" and (g_verb == "*" or p_verb != g_verb):
        return False
    return True


def _matching_grants(grants: Iterable[str], leg: dict) -> list[str]:
    include, exclude = leg.get("include", []), leg.get("exclude", [])
    return sorted({
        g for g in grants
        if any(_overlap(g, p) for p in include)
        and not any(_covers(p, g) for p in exclude)
    })


# ── profilo di un agente ──────────────────────────────────────────────────

def has_shell(spec) -> bool:
    """L'agente può eseguire comandi di shell (→ il gate è aggirabile)."""
    sandbox = getattr(spec, "sandbox", None)
    allow = list(getattr(sandbox, "allow_shell_cmds", None) or [])
    deny = list(getattr(sandbox, "deny_shell_patterns", None) or [])
    if not allow:
        return False
    if any(d.strip() in ("*", "**") for d in deny):
        return False
    return True


def agent_profile(spec, config: Optional[dict] = None) -> dict:
    """Profilo trifecta di un singolo agente, dai suoi grant effettivi.

    Ritorna anche i grant che hanno acceso ciascun lato: il numero da solo non
    è azionabile, la scomposizione sì (è il requisito del dialog nell'issue)."""
    cfg = config or load_config()
    name = getattr(spec, "name", "?")
    kind = getattr(spec, "type", "normal")
    if kind == "human":
        # Un principal umano non esegue tool: non porta capacità nel canale.
        return {"name": name, "type": kind, "human": True, "score": 0,
                "legs": {leg: False for leg in LEGS},
                "why": {leg: [] for leg in LEGS},
                "shell": False, "expands": False}
    grants = [str(g).strip() for g in (getattr(spec, "tool_permissions", None) or [])
              if str(g).strip()]
    legs, why = {}, {}
    for leg in LEGS:
        matched = _matching_grants(grants, cfg[leg])
        legs[leg] = bool(matched)
        why[leg] = matched[:_MAX_REASONS]
    return {
        "name": name,
        "type": kind,
        "human": False,
        "score": sum(1 for leg in LEGS if legs[leg]),
        "legs": legs,
        "why": why,
        "shell": has_shell(spec),
        "expands": bool(_matching_grants(grants, cfg["expansion"])),
    }


# ── profilo di un contesto (canale, DM) ───────────────────────────────────

def context_profile(participants: Iterable[str],
                    specs: Optional[Iterable] = None,
                    config: Optional[dict] = None) -> dict:
    """Danger score 0–3 di un contesto: OR dei lati sulla **chiusura**.

    `participants` sono i nomi dichiarati nel meta del topic; `specs` l'elenco
    completo degli agenti registrati (default: la registry). Un partecipante
    sconosciuto alla registry è ignorato — non si inventa un profilo.
    """
    cfg = config or load_config()
    if specs is None:
        from .loader import registry  # import locale: evita cicli all'avvio
        specs = registry.list()
    by_name = {getattr(s, "name", None): s for s in (specs or []) if s is not None}

    members = [by_name[p] for p in dict.fromkeys(participants or []) if p in by_name]
    profiles = [agent_profile(s, cfg) for s in members]
    unknown = [p for p in dict.fromkeys(participants or []) if p not in by_name]

    # Chiusura: chi può aggiungere partecipanti porta potenzialmente nel canale
    # QUALUNQUE agente registrato. Il punteggio si calcola su quell'insieme,
    # ma i due contributi restano distinti perché il numero sia spiegabile.
    expanded_by = [p["name"] for p in profiles if p["expands"]]
    reachable: list[dict] = []
    if expanded_by:
        member_names = {p["name"] for p in profiles}
        reachable = [agent_profile(s, cfg) for n, s in sorted(by_name.items())
                     if n not in member_names
                     and getattr(s, "type", "normal") != "human"]

    closure = profiles + reachable
    legs = {leg: any(p["legs"][leg] for p in closure) for leg in LEGS}
    score = sum(1 for leg in LEGS if legs[leg])
    shell_agents = [p["name"] for p in closure if p["shell"]]
    # Punteggio dei soli partecipanti DICHIARATI. Serve a non appiattire tutto:
    # se il canale è a 3 solo perché qualcuno può invitare, il numero da solo
    # sarebbe indistinguibile da un canale già a 3 con i presenti.
    direct_legs = {leg: any(p["legs"][leg] for p in profiles) for leg in LEGS}
    direct_score = sum(1 for leg in LEGS if direct_legs[leg])
    return {
        "score": score,
        "label": f"{score}/3",
        "symbol": SYMBOLS.get(score, "⚠️"),
        "legs": legs,
        "direct": {
            "score": direct_score,
            "label": f"{direct_score}/3",
            "legs": direct_legs,
            "by_leg": {leg: [p["name"] for p in profiles if p["legs"][leg]] for leg in LEGS},
            # Chi ha la shell ED è già nel canale: distinto dai soli raggiungibili,
            # altrimenti la UI attribuirebbe la shell a un agente che non c'è.
            "shell_agents": [p["name"] for p in profiles if p["shell"]],
        },
        # Chi accende cosa: la UI mostra «legge il web: clodia · dati privati:
        # ophelia · può inviare: messaggero» invece del solo numero.
        "by_leg": {leg: [p["name"] for p in closure if p["legs"][leg]] for leg in LEGS},
        "agents": profiles,
        "reachable": [p["name"] for p in reachable],
        "expanded_by": expanded_by,
        # Flag separato, NON un quarto lato: dice che il controllo è aggirabile.
        "shell": bool(shell_agents),
        "shell_agents": shell_agents,
        "unknown_participants": unknown,
        "config_version": cfg.get("version", 0),
    }
