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
- **La capacità e il rischio residuo sono due numeri diversi.** `score` dice
  quali verbi l'agente ha; `residual` dice cosa resta dopo le mitigazioni
  APPLICATE — la whitelist di destinazione (#104 §7) e, da #126, il M-gate: un
  lato acceso soltanto da verbi che richiedono conferma umana a ogni chiamata
  non è un lato che l'agente attraversa da solo. Nessuna delle due mitigazioni
  tocca `score`: confondere capacità e confinamento renderebbe illeggibili
  entrambi.
- **Il default è fail-CLOSED** (#119): un namespace che il catalogo non conosce
  si assume capace di leggere dati privati e di farli uscire. Prima era
  fail-open, e un agente i cui soli grant fossero verbi di un connettore nuovo
  risultava «innocuo» pur potendo esfiltrare. È anche la condizione perché la
  whitelist di destinazione funzioni: il PDP confronta la destinazione solo per
  i verbi che *sa* essere di uscita.

I principal `human` non contribuiscono ad alcun lato: non eseguono tool. Sono
l'unico declassificatore legittimo, non una capacità del canale.
"""
from __future__ import annotations

import logging
import os
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
    """Grant che accendono `leg`, al netto delle NEGAZIONI nei grant stessi.

    Un grant che inizia con `-` è una sottrazione: `topic.*` più
    `-topic.remote_push` significa «tutto topic tranne quel verbo». Serve perché
    la §8 di #104 toglie singoli verbi da un namespace concesso in blocco, e senza
    questo lo scorer vedeva `topic.*` combaciare con `topic.remote_push` nel
    catalogo e continuava ad accendere l'uscita — enforcement applicato, misura
    cieca. È la divergenza peggiore: un numero che descrive un sistema diverso da
    quello che gira.

    La sottrazione agisce sui pattern PUNTUALI del catalogo. Negare
    `topic.remote_push` spegne quella voce; non spegne un `topic.*` presente
    negli include, perché un namespace intero non è coperto dalla negazione di un
    suo verbo — sarebbe una falsa rassicurazione.
    """
    include, exclude = leg.get("include", []), leg.get("exclude", [])
    pos = [g for g in grants if not str(g).startswith("-")]
    neg = {str(g)[1:].strip() for g in grants if str(g).startswith("-")}
    out = set()
    for g in pos:
        for pat in include:
            if not _overlap(g, pat):
                continue
            if any(_covers(x, g) for x in exclude):
                continue
            # Se il pattern del catalogo è PUNTUALE ed è negato nei grant, quella
            # voce non accende: il verbo non è raggiungibile.
            if "*" not in str(pat) and str(pat) in neg:
                continue
            # Se il GRANT è puntuale e negato, non accende nulla.
            if g in neg:
                continue
            out.add(g)
            break
    return sorted(out)


# ── confinamento in uscita (#104 §7 proprietà 4) ──────────────────────────
# «Uscita circoscritta e uscita arbitraria non sono lo stesso rischio, anche se
# oggi il calcolatore dà 3/3 a entrambi.» Il dato sta nella config del GATEWAY,
# su un volume che l'agent-server non monta di proposito (#80: chi può
# riscrivere la whitelist si auto-concede le destinazioni), quindi si legge dal
# canale server-to-server. Cache breve: cambia quando un umano approva una
# destinazione, non a ogni richiesta.
_EGRESS_TTL = 30.0
_EGRESS_CACHE: tuple[float, dict] | None = None

#: Come il MODO del gateway qualifica l'uscita. Un confinamento che non è
#: applicato non è un confinamento: contarlo abbasserebbe il punteggio di un
#: agente che può ancora inviare liberamente, cioè mentirebbe nella direzione
#: peggiore. `report` e `off` non confinano nulla.
_ENFORCING_MODES = ("gate", "on")


def _gateway_get(path: str, fallback: dict, what: str) -> dict:
    """GET server-to-server sul gateway, o `fallback` se non si può leggere.

    Il fallback non è un dettaglio di robustezza: è la direzione dell'errore.
    Ogni dato che si legge da qui descrive una MITIGAZIONE, e una mitigazione
    immaginata abbassa il rischio di un agente che invece agisce da solo. Se il
    gateway non risponde si conta zero mitigazione, non la mitigazione sperata.
    """
    secret = (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip()
    if not secret:
        return fallback
    try:
        import httpx
        mcp = os.environ.get("CLODIA_TOOLS_MCP_URL",
                             "http://clodia-tools:7849/mcp/").rstrip("/")
        base = mcp[:-len("/mcp")] if mcp.endswith("/mcp") else mcp
        r = httpx.get(f"{base}{path}",
                      headers={"X-Orchestrator-Secret": secret}, timeout=4.0)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001 — misura, non enforcement
        LOG.warning("trifecta: %s non leggibile (%s)", what, e)
        return fallback


def egress_confinement(force: bool = False) -> dict:
    """{mode, agents: {name: {type: {scope, count}}}} dal gateway.

    Best-effort: se il gateway non risponde si ritorna un confinamento VUOTO,
    che significa «non so, quindi non conto nessun confinamento» — il punteggio
    resta quello della capacità pura. L'errore va in questa direzione: un
    confinamento immaginato è peggio di un confinamento non visto.
    """
    global _EGRESS_CACHE
    import time
    now = time.monotonic()
    if _EGRESS_CACHE and not force and now - _EGRESS_CACHE[0] < _EGRESS_TTL:
        return _EGRESS_CACHE[1]
    out = _gateway_get("/internal/egress", {"mode": "unknown", "agents": {}},
                       "confinamento in uscita")
    _EGRESS_CACHE = (now, out)
    return out


def egress_scope(name: str, conf: dict, egress_lit: bool) -> str:
    """Qualifica l'uscita di un agente: `none` · `presided` · `listed` · `arbitrary`.

    - `none`      nessun verbo di uscita, o tutti i tipi dichiarati muti;
    - `presided`  il gateway è in modo `gate`: una destinazione non in whitelist
                  passa da un umano. È lo stato più forte in pratica, perché non
                  richiede di aver previsto le destinazioni;
    - `listed`    modo `on` con regole esplicite: solo destinazioni dichiarate;
    - `arbitrary` nessun confinamento applicato, o una regola `*`.
    """
    if not egress_lit:
        return "none"
    mode = str(conf.get("mode") or "unknown")
    if mode not in _ENFORCING_MODES:
        return "arbitrary"
    types = (conf.get("agents") or {}).get(name)
    if types is None:
        # Agente senza voce nella config del gateway. In modo `gate` una
        # destinazione qualunque passa comunque da un umano: è presidiata.
        return "presided" if mode == "gate" else "arbitrary"
    scopes = {v.get("scope") for v in types.values() if isinstance(v, dict)}
    if "wide" in scopes:
        return "arbitrary"
    if scopes and scopes <= {"muted"}:
        return "none"
    return "presided" if mode == "gate" else "listed"


# ── supervisione umana per verbo (M-gate, #126) ───────────────────────────
# «Il residuo scontava solo la whitelist di DESTINAZIONE; il gate sui verbi no.»
# Un verbo gated richiede una conferma umana a OGNI chiamata: un lato acceso solo
# da verbi così non è un lato che l'agente attraversa da solo, ed è la stessa
# forma di mitigazione che `egress_scope != "arbitrary"` già sconta.
#
# L'insieme gated si LEGGE dal gateway invece di duplicarlo qui. È deliberato:
# `gate.py` lo rende configurabile per istanza (`CLODIA_GATED_VERBS`), e una
# copia nel codice divergerebbe in silenzio dal gate applicato. È esattamente la
# divergenza già pagata in #195 — un numero che descrive un sistema diverso da
# quello che gira — e lì almeno il codice era nello stesso repo.
_GATE_TTL = 30.0
_GATE_CACHE: tuple[float, dict] | None = None

#: «Non so quali verbi siano gated» → nessuno sconto. Vedi `_gateway_get`.
_NO_GATE = {"prefixes": [], "exact": []}


def gated_verbs(force: bool = False) -> dict:
    """`{prefixes, exact}` dell'insieme gated EFFETTIVO del gateway.

    Best-effort come `egress_confinement()`: gateway muto o troppo vecchio per
    conoscere la rotta → insieme vuoto → il residuo resta quello della capacità
    pura. Nessun ordine di deploy fra i due repo è quindi obbligatorio.
    """
    global _GATE_CACHE
    import time
    now = time.monotonic()
    if _GATE_CACHE and not force and now - _GATE_CACHE[0] < _GATE_TTL:
        return _GATE_CACHE[1]
    raw = _gateway_get("/internal/gate/spec", _NO_GATE, "insieme dei verbi gated")
    out = {"prefixes": [str(p) for p in (raw.get("prefixes") or [])],
           "exact": [str(v) for v in (raw.get("exact") or [])]}
    _GATE_CACHE = (now, out)
    return out


def grant_is_gated(grant: str, gated: dict) -> bool:
    """True se OGNI verbo che `grant` può raggiungere richiede conferma umana.

    Più severo di `is_gated()` del gateway, che risponde su un verbo singolo,
    perché un grant è un INSIEME di verbi. `topic.*` include
    `topic.add_participant` (gated) e `topic.read_file` (non gated): presidiato
    per un decimo, e contarlo come presidiato sarebbe la falsa rassicurazione che
    questa misura non può permettersi. Contano quindi solo il verbo puntuale
    gated e il namespace la cui FAMIGLIA intera lo è (`settings.`, `pki.`, `ca.`).

    `*` non è gated: copre anche tutti i verbi che nessuno presidia.
    """
    g = str(grant).strip()
    if not g or g.startswith("-"):
        return False   # una negazione non concede nulla, non c'è niente da presidiare
    prefixes = tuple(str(p) for p in (gated.get("prefixes") or ()))
    exact = frozenset(str(v) for v in (gated.get("exact") or ()))
    ns, verb = _split(g)
    if ns == "*":
        return False
    if verb == "*":
        # Namespace intero: presidiato solo se il gate copre la FAMIGLIA. Non
        # basta che qualche verbo del namespace sia in `exact`.
        return f"{ns}." in prefixes
    full = f"{ns}.{verb}"
    return full in exact or any(full.startswith(p) for p in prefixes)


# ── fail-closed sui namespace non classificati (#119) ─────────────────────
#: I lati che si ASSUMONO su un namespace ignoto. Non tutti e tre di proposito:
#: `untrusted_input` marcherebbe ogni pack nuovo a 3/3 all'istante, che è rumore
#: e non informazione. `private_data` + `egress` sono la coppia che **chiude un
#: flusso** — «può leggere le tue cose e mandarle fuori» — ed è la coppia su cui
#: whitelist e gate possono agire.
_UNKNOWN_NS_LEGS = ("private_data", "egress")


def _known_namespaces(cfg: dict) -> set[str]:
    """Namespace che il catalogo CONOSCE, presi da tutti i pattern di tutti i
    lati (include ed exclude) più l'espansione.

    Gli `exclude` contano: `gdrive.rename` è deliberatamente in nessun lato, e
    `gdrive` va considerato noto — la regola riguarda l'IGNOTO, non l'escluso.
    """
    out: set[str] = set()
    for key in (*LEGS, "expansion"):
        for field in ("include", "exclude"):
            for pat in cfg.get(key, {}).get(field, []) or []:
                ns, _ = _split(str(pat).lstrip("-"))
                if ns and ns != "*":
                    out.add(ns)
    return out


def unclassified_namespaces(grants: Iterable[str], cfg: dict) -> list[str]:
    """Namespace dei grant che il catalogo non classifica.

    Il difetto misurato in #119: un agente i cui soli grant erano
    `slack.post_message` / `dropbox.upload` risultava **0/3**, cioè innocuo, pur
    potendo esfiltrare. Il default del catalogo era fail-OPEN: un namespace
    ignoto non accendeva nulla. Due conseguenze, ed è la seconda che conta di
    più: il punteggio invecchiava da solo a ogni pack nuovo, **e** la whitelist
    di destinazione non si attivava affatto, perché il PDP confronta la
    destinazione solo per i verbi che sa essere di uscita. Un verbo non
    classificato passava sotto il punteggio e sotto la whitelist insieme.
    """
    known = _known_namespaces(cfg)
    out: set[str] = set()
    for g in grants:
        # Una NEGAZIONE (`-verbo`) non concede niente: leggerla come namespace
        # produceva un `-topic` ignoto, e il fail-closed accendeva due lati su un
        # grant che ne TOGLIE uno. Due difese corrette che si sommavano male.
        if str(g).startswith("-"):
            continue
        ns, _ = _split(str(g))
        # `*` combacia con ogni pattern classificato: non è un ignoto, è il
        # contrario — accende tutto da sé.
        if ns and ns != "*" and ns not in known:
            out.add(ns)
    return sorted(out)


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


def agent_profile(spec, config: Optional[dict] = None,
                  egress_conf: Optional[dict] = None,
                  gated: Optional[dict] = None) -> dict:
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
                "shell": False, "expands": False, "unclassified": [],
                "egress_scope": "none", "residual": 0,
                "gated_legs": {leg: False for leg in LEGS},
                "residual_legs": {leg: False for leg in LEGS},
                "ungated": {leg: [] for leg in LEGS}}
    grants = [str(g).strip() for g in (getattr(spec, "tool_permissions", None) or [])
              if str(g).strip()]
    legs, why = {}, {}
    # I grant che accendono ciascun lato, INTERI: `why` è troncato per la UI, e lo
    # sconto del gate deve vederli tutti — basta un grant non presidiato perché il
    # lato resti attraversabile.
    lit_by: dict[str, list[str]] = {}
    for leg in LEGS:
        matched = _matching_grants(grants, cfg[leg])
        legs[leg] = bool(matched)
        lit_by[leg] = list(matched)
        why[leg] = matched[:_MAX_REASONS]
    # FAIL-CLOSED (#119): un namespace che il catalogo non conosce si assume
    # capace di leggere dati privati e di farli uscire. Il costo di sbagliare in
    # questa direzione è un falso positivo su un connettore innocuo, che si
    # corregge con una riga di catalogo; il costo opposto è un canale di
    # esfiltrazione invisibile sia al punteggio sia alla whitelist.
    unknown = unclassified_namespaces(grants, cfg)
    if unknown:
        # I grant che stanno accendendo i lati per via del fail-closed. Servono
        # allo sconto del gate come gli altri: un `pki.qualcosa` ignoto al
        # catalogo accende due lati, ma resta un verbo che passa da un umano, e la
        # regola è una sola per tutti i grant.
        unknown_grants = [g for g in grants if not g.startswith("-")
                          and _split(g)[0] in unknown]
        for leg in _UNKNOWN_NS_LEGS:
            legs[leg] = True
            lit_by[leg] = lit_by[leg] + [g for g in unknown_grants
                                         if g not in lit_by[leg]]
            # La motivazione dice PERCHÉ: «acceso da email.send» e «acceso
            # perché slack.* è ignoto al catalogo» richiedono azioni diverse, e
            # senza la distinzione l'operatore non sa quale.
            reasons = why[leg] + [f"{ns}.* (namespace non classificato)"
                                  for ns in unknown]
            why[leg] = reasons[:_MAX_REASONS]
    conf = egress_conf if egress_conf is not None else egress_confinement()
    scope = egress_scope(name, conf, legs["egress"])
    # #126 — sconto del M-gate, per lato. Un lato è presidiato solo se OGNI grant
    # che lo accende richiede conferma umana: uno solo non presidiato e il lato
    # resta attraversabile in autonomia.
    gspec = gated if gated is not None else gated_verbs()
    ungated = {leg: [g for g in lit_by[leg] if not grant_is_gated(g, gspec)]
               for leg in LEGS}
    gated_legs = {leg: bool(lit_by[leg]) and not ungated[leg] for leg in LEGS}
    # Le due mitigazioni sono alternative, non cumulative: presidiare l'uscita
    # basta, che avvenga per destinazione (whitelist) o per verbo (gate).
    mitigated = {"private_data": gated_legs["private_data"],
                 "untrusted_input": gated_legs["untrusted_input"],
                 "egress": gated_legs["egress"] or scope != "arbitrary"}
    residual_legs = {leg: bool(legs[leg]) and not mitigated[leg] for leg in LEGS}
    return {
        "name": name,
        "type": kind,
        "human": False,
        "score": sum(1 for leg in LEGS if legs[leg]),
        "legs": legs,
        "why": why,
        "shell": has_shell(spec),
        "expands": bool(_matching_grants(grants, cfg["expansion"])),
        "unclassified": unknown,
        # §7 proprietà 4: il punteggio da solo dava 3/3 sia a un'uscita
        # arbitraria sia a una circoscritta. `score` resta la CAPACITÀ (non
        # mente: quei verbi ce li ha), `residual` è il rischio che rimane dopo il
        # confinamento applicato — l'uscita conta solo se è arbitraria.
        "egress_scope": scope,
        # #126: lati accesi SOLO da verbi gated (conferma umana a ogni chiamata).
        "gated_legs": gated_legs,
        # Perché un lato NON è scontato: i grant che lo accendono senza passare da
        # un umano. È la parte azionabile — dice quali grant togliere per
        # guadagnare lo sconto, e senza di essa un lato «presidiato al 90%» e uno
        # per nulla presidiato sarebbero indistinguibili nel report.
        "ungated": {leg: ungated[leg][:_MAX_REASONS] for leg in LEGS},
        # I lati che restano dopo TUTTE le mitigazioni applicate.
        "residual_legs": residual_legs,
        "residual": sum(1 for leg in LEGS if residual_legs[leg]),
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

    # UNA sola lettura di confinamento e insieme gated per tutto il canale: la
    # chiusura può includere decine di agenti, e il dato è lo stesso per tutti.
    conf = egress_confinement()
    gspec = gated_verbs()
    members = [by_name[p] for p in dict.fromkeys(participants or []) if p in by_name]
    profiles = [agent_profile(s, cfg, conf, gspec) for s in members]
    unknown = [p for p in dict.fromkeys(participants or []) if p not in by_name]

    # Chiusura: chi può aggiungere partecipanti porta potenzialmente nel canale
    # QUALUNQUE agente registrato. Il punteggio si calcola su quell'insieme,
    # ma i due contributi restano distinti perché il numero sia spiegabile.
    expanded_by = [p["name"] for p in profiles if p["expands"]]
    reachable: list[dict] = []
    if expanded_by:
        member_names = {p["name"] for p in profiles}
        reachable = [agent_profile(s, cfg, conf, gspec)
                     for n, s in sorted(by_name.items())
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
        # Namespace non classificati presenti nella chiusura (#119): un canale a
        # 3/3 «perché nessuno ha classificato slack» è un problema di catalogo,
        # non di composizione, e va distinto — le due cose si risolvono con
        # azioni diverse (una riga di yaml vs togliere un partecipante).
        "unclassified": sorted({ns for pr in closure
                                for ns in (pr.get("unclassified") or [])}),
        # §7 proprietà 4 a livello di canale. `score` è la capacità presente;
        # `residual` è ciò che resta dopo il confinamento applicato. Un canale a
        # 3/3 in cui ogni destinazione nuova passa da un umano NON è lo stesso
        # rischio di un canale a 3/3 che può scrivere a chiunque, e prima i due
        # erano indistinguibili.
        "egress_mode": conf.get("mode", "unknown"),
        "egress_scopes": sorted({pr.get("egress_scope", "none") for pr in closure
                                 if pr.get("legs", {}).get("egress")}),
        # #126: l'insieme gated è stato letto? Un canale senza sconti perché il
        # gateway non risponde non è un canale senza gate, e le due letture
        # richiedono azioni opposte (guardare la composizione vs. guardare il
        # gateway). Senza il flag il report le confonderebbe.
        "gate_visible": bool(gspec.get("prefixes") or gspec.get("exact")),
        # Lati presidiati per OGNI agente della chiusura che li accende: se un
        # solo agente li attraversa in autonomia, il canale non è presidiato.
        "gated_legs": {leg: legs[leg] and not any(pr["residual_legs"][leg]
                                                 for pr in closure)
                       for leg in LEGS},
        # Si calcola sull'OR dei lati come `score`, NON come massimo dei residui
        # per-agente: un agente a 2/3 senza uscita più uno a 1/3 con uscita
        # arbitraria fanno un canale a 3 residuo, mentre il massimo dei residui
        # direbbe 2. La chiusura è l'unità di valutazione, anche qui. L'OR è sui
        # lati RESIDUI dei singoli, perché una mitigazione è per-agente: presidiare
        # l'uscita di uno non presidia quella di un altro.
        "residual_legs": {leg: any(pr["residual_legs"][leg] for pr in closure)
                          for leg in LEGS},
        "residual": sum(1 for leg in LEGS
                        if any(pr["residual_legs"][leg] for pr in closure)),
        "config_version": cfg.get("version", 0),
    }
