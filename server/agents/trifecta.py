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
    out = {"mode": "unknown", "agents": {}}
    secret = (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip()
    if secret:
        try:
            import httpx
            mcp = os.environ.get("CLODIA_TOOLS_MCP_URL",
                                 "http://clodia-tools:7849/mcp/").rstrip("/")
            base = mcp[:-len("/mcp")] if mcp.endswith("/mcp") else mcp
            r = httpx.get(f"{base}/internal/egress",
                          headers={"X-Orchestrator-Secret": secret}, timeout=4.0)
            r.raise_for_status()
            out = r.json()
        except Exception as e:  # noqa: BLE001 — misura, non enforcement
            LOG.warning("trifecta: confinamento in uscita non leggibile (%s)", e)
    _EGRESS_CACHE = (now, out)
    return out


def remote_uri(meta: dict) -> Optional[str]:
    """URI di destinazione del REMOTE del canale, o None se non ne ha.

    Un remote non è un verbo: è un condotto **permanente** verso l'esterno. Un
    topic collegato a una cartella Drive fa uscire i propri file da lì per
    definizione, e se quella cartella non è fra le destinazioni vagliate l'uscita
    è arbitraria — indipendentemente da quali verbi abbiano i partecipanti.
    """
    rem = (meta or {}).get("remote") or {}
    rtype = str(rem.get("type") or "").strip()
    cfg = rem.get("config") or {}
    if rtype == "drive":
        folder = str(cfg.get("folder") or "").strip()
        return f"gdrive:folder/{folder}" if folder else None
    if rtype == "git":
        url = str(cfg.get("url") or "").strip()
        return url or None
    return None


def uri_allowed(uri: Optional[str]) -> Optional[bool]:
    """`True`/`False` se l'URI è fra le destinazioni vagliate, `None` se non si sa.

    Query di APPARTENENZA al gateway, non lettura della lista: il punteggio non
    deve ricevere gli indirizzi (una rubrica è dato privato). Chiedendo di un URI
    che conosce già — viene dal meta del topic — non impara nulla di nuovo.

    `None` su gateway irraggiungibile: non si inventa né sì né no. Il chiamante
    decide, e la scelta prudente è considerarlo non vagliato — un condotto verso
    l'esterno che non sappiamo se è approvato va mostrato, non nascosto.
    """
    if not uri:
        return None
    secret = (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip()
    if not secret:
        return None
    try:
        import httpx
        mcp = os.environ.get("CLODIA_TOOLS_MCP_URL",
                             "http://clodia-tools:7849/mcp/").rstrip("/")
        base = mcp[:-len("/mcp")] if mcp.endswith("/mcp") else mcp
        r = httpx.get(f"{base}/internal/egress", params={"uri": uri},
                      headers={"X-Orchestrator-Secret": secret}, timeout=4.0)
        r.raise_for_status()
        return bool(r.json().get("allowed"))
    except Exception as e:  # noqa: BLE001 — misura, non enforcement
        LOG.warning("trifecta: appartenenza di '%s' non verificabile (%s)", uri, e)
        return None


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
    # La whitelist è GLOBALE (#128): la forma arriva in `egress`, non più per
    # agente. Questa funzione leggeva ancora `conf["agents"][name]`, che dopo quel
    # cambio è sempre assente — con modo `gate` usciva "presided" per caso giusto,
    # ma un `*` non veniva più rilevato: una falsa rassicurazione, cioè la
    # direzione d'errore che questa misura non può permettersi.
    shape = str(((conf.get("egress") or {}).get("scope")) or "unknown")
    if shape == "wide":
        # Una lista che contiene `*` è dichiarata e non vincola niente.
        return "arbitrary"
    if shape in ("none", "muted"):
        # Nessuna destinazione dichiarata. In `gate` ogni invio passa comunque da
        # un umano — presidiata; in `on` non passa affatto — nessuna uscita.
        return "presided" if mode == "gate" else "none"
    if shape == "unknown":
        # Forma non leggibile (gateway muto): non si inventa un confinamento.
        return "arbitrary"
    return "presided" if mode == "gate" else "listed"


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
                  all_specs=None) -> dict:
    """Profilo trifecta di un singolo agente, dai suoi grant effettivi.

    ⚠️ `score` e `residual` di QUESTO profilo non sono una metrica di prodotto e
    non vanno mostrati accanto a un agente. Da quando il punteggio conta i bit del
    vettore (#198), due dei tre bit sono proprietà del CANALE: la contaminazione è
    un evento della stanza, e l'uscita arbitraria dipende da una whitelist globale
    (#128). Un numero per-agente somma cose che all'agente non appartengono, e
    suggerisce che l'agente sia il soggetto della misura — che è la lettura
    abbandonata in #77 («l'unità di valutazione non è l'agente, è il contesto»).

    Ciò che di questo profilo resta vero e utile è `legs` — quali capacità
    l'agente PORTA — e `why`, che dice con quali grant. Il numero esiste solo come
    passaggio intermedio per `context_profile`.
    

    Ritorna anche i grant che hanno acceso ciascun lato: il numero da solo non
    è azionabile, la scomposizione sì (è il requisito del dialog nell'issue)."""
    cfg = config or load_config()
    name = getattr(spec, "name", "?")
    kind = getattr(spec, "type", "bot")
    if kind == "human":
        # Un principal umano non esegue tool: non porta capacità nel canale.
        return {"name": name, "type": kind, "human": True, "score": 0,
                "legs": {leg: False for leg in LEGS},
                "why": {leg: [] for leg in LEGS},
                "shell": False, "expands": False, "unclassified": [],
                "egress_scope": "none", "residual": 0}
    # I verbi EFFETTIVI, non quelli dichiarati: dall'8 ago 2026 un seed eredita
    # dall'arciseed, e leggere la sola dichiarazione SOTTOSTIMA il rischio.
    # Misurato: ripulendo i seed dai verbi ridondanti, il punteggio di
    # `segretario` è sceso da 2 a 0 — un file più pulito non rende un agente meno
    # pericoloso, e un segnale di sicurezza che si abbassa da solo è la forma
    # peggiore di errore silenzioso.
    from .inheritance import effective_tool_permissions
    try:
        _tutti = all_specs
        if _tutti is None:
            # Nessun chiamante deve poter ottenere un profilo che sottostima solo
            # perché non ha passato l'elenco: si legge dal registry.
            from . import registry as _reg
            _tutti = _reg.list()
        _specs = {getattr(s, "name", None): s for s in (_tutti or [])}
        _specs.pop(None, None)
        _specs.setdefault(name, spec)
        grants = [g for g in effective_tool_permissions(name, _specs) if g]
    except Exception as e:  # noqa: BLE001 — un profilo non calcolabile non deve
        # rompere il canale; si ricade sulla dichiarazione, che sottostima ma non
        # esplode, e si logga perché quel numero va guardato con sospetto.
        LOG.warning("verbi ereditati non risolti per '%s' (%s): profilo sulla "
                    "sola dichiarazione", name, type(e).__name__)
        grants = [str(g).strip() for g in (getattr(spec, "tool_permissions", None) or [])
                  if str(g).strip()]
    legs, why = {}, {}
    for leg in LEGS:
        matched = _matching_grants(grants, cfg[leg])
        legs[leg] = bool(matched)
        why[leg] = matched[:_MAX_REASONS]
    # FAIL-CLOSED (#119): un namespace che il catalogo non conosce si assume
    # capace di leggere dati privati e di farli uscire. Il costo di sbagliare in
    # questa direzione è un falso positivo su un connettore innocuo, che si
    # corregge con una riga di catalogo; il costo opposto è un canale di
    # esfiltrazione invisibile sia al punteggio sia alla whitelist.
    unknown = unclassified_namespaces(grants, cfg)
    if unknown:
        for leg in _UNKNOWN_NS_LEGS:
            legs[leg] = True
            # La motivazione dice PERCHÉ: «acceso da email.send» e «acceso
            # perché slack.* è ignoto al catalogo» richiedono azioni diverse, e
            # senza la distinzione l'operatore non sa quale.
            reasons = why[leg] + [f"{ns}.* (namespace non classificato)"
                                  for ns in unknown]
            why[leg] = reasons[:_MAX_REASONS]
    conf = egress_conf if egress_conf is not None else egress_confinement()
    scope = egress_scope(name, conf, legs["egress"])
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
        "residual": (sum(1 for leg in LEGS if legs[leg])
                     - (1 if legs["egress"] and scope != "arbitrary" else 0)),
    }


# ── profilo di un contesto (canale, DM) ───────────────────────────────────

def context_profile(participants: Iterable[str],
                    specs: Optional[Iterable] = None,
                    config: Optional[dict] = None,
                    tainted: Optional[bool] = None,
                    remote_egress: Optional[bool] = None,
                    channel_private_data: Optional[bool] = None) -> dict:
    """Danger score 0–3 di un contesto = **numero di bit accesi del vettore**.

    Il vettore (clodia-platform#104 §4, formalizzato il 3 ago 2026):

        1 0 0   è ENTRATO contenuto non fidato in questo canale
        0 1 0   qualcuno qui ha accesso a dati privati
        0 0 1   qualcuno qui può scrivere verso un sistema esterno NON approvato

    Il primo bit è un **evento**, gli altri due **proprietà**. Prima questo
    punteggio contava tre CAPACITÀ, fra cui «può leggere contenuto non fidato» —
    che è quasi sempre vera e non dice niente su cosa sia successo. Un canale in
    cui l'owner ha lavorato solo con i propri documenti risultava 3/3 come uno in
    cui è entrato un PDF di terzi: il numero non discriminava, ed era l'unico
    lavoro che gli si chiedeva.

    Il terzo bit conta l'uscita **arbitraria**: se ogni destinazione nuova passa
    da un umano, il flusso non si chiude da sé e il bit è spento. `capability`
    resta esposto accanto — i verbi ci sono davvero, e negarlo sarebbe la sola
    bugia che questa misura non può permettersi.

    `tainted=None` = non leggibile (gateway giù): il bit vale `?`, si conta 0 e il
    chiamante lo dichiara. Inventarlo a 1 sarebbe un falso allarme, a 0 una falsa
    rassicurazione: l'unica risposta onesta è «non lo so».

    `participants` sono i nomi dichiarati nel meta del topic; `specs` l'elenco
    completo degli agenti registrati (default: la registry). Un partecipante
    sconosciuto alla registry è ignorato — non si inventa un profilo.
    """
    cfg = config or load_config()
    if specs is None:
        from .loader import registry  # import locale: evita cicli all'avvio
        specs = registry.list()
    by_name = {getattr(s, "name", None): s for s in (specs or []) if s is not None}

    # UNA sola lettura del confinamento per tutto il canale: la chiusura può
    # includere decine di agenti, e il dato è lo stesso per tutti.
    conf = egress_confinement()
    members = [by_name[p] for p in dict.fromkeys(participants or []) if p in by_name]
    profiles = [agent_profile(s, cfg, conf) for s in members]
    unknown = [p for p in dict.fromkeys(participants or []) if p not in by_name]

    # Chiusura: chi può aggiungere partecipanti porta potenzialmente nel canale
    # QUALUNQUE agente registrato. Il punteggio si calcola su quell'insieme,
    # ma i due contributi restano distinti perché il numero sia spiegabile.
    expanded_by = [p["name"] for p in profiles if p["expands"]]
    reachable: list[dict] = []
    if expanded_by:
        member_names = {p["name"] for p in profiles}
        reachable = [agent_profile(s, cfg, conf) for n, s in sorted(by_name.items())
                     if n not in member_names
                     and getattr(s, "type", "bot") != "human"]

    closure = profiles + reachable
    legs = {leg: any(p["legs"][leg] for p in closure) for leg in LEGS}
    # CAPACITÀ: l'OR dei tre lati, come prima. Resta esposta, non è il titolo.
    capability = sum(1 for leg in LEGS if legs[leg])
    # I tre bit del vettore. Il secondo è capacità, il terzo capacità NETTA del
    # confinamento applicato, il primo un fatto avvenuto.
    # Terzo bit: uscita arbitraria dei partecipanti OPPURE un remote del canale
    # che punta a una destinazione non vagliata. La seconda non dipende dai verbi
    # di nessuno — è il canale stesso ad avere un condotto verso l'esterno.
    arbitrary_egress = bool(remote_egress) or (legs["egress"] and any(
        p.get("egress_scope") == "arbitrary" for p in closure))
    # Secondo bit: un FATTO SUL CANALE, non una capacità dei presenti (definizione
    # dell'owner, 17 ago 2026 — decision record 36):
    #
    #   «il secondo bit setta se al canale sono stati aggiunti dati di natura
    #    riservata e non generati dagli agenti, ad esempio un file uploaded
    #    oppure un attachment di email, oppure un collegamento ad un remote»
    #
    # Prima era l'OR delle capacità di lettura dei partecipanti, e per questo era
    # quasi sempre acceso: qualunque agente che possa stare in un canale ha i
    # verbi per leggerne i file. Un bit acceso su tutto non discrimina, ed era
    # l'unico lavoro che gli si chiedeva.
    #
    # `None` = il chiamante non l'ha calcolato (o il gateway è muto): si ricade
    # sulla capacità, che è la direzione prudente. Un `0` inventato sarebbe una
    # rassicurazione su dati che potrebbero esserci.
    private_data = (legs["private_data"] if channel_private_data is None
                    else bool(channel_private_data))
    bits = (1 if tainted else 0, 1 if private_data else 0,
            1 if arbitrary_egress else 0)
    score = sum(bits)
    vector = ("?" if tainted is None else str(bits[0])) + str(bits[1]) + str(bits[2])
    shell_agents = [p["name"] for p in closure if p["shell"]]
    # Punteggio dei soli partecipanti DICHIARATI. Serve a non appiattire tutto:
    # se il canale è a 3 solo perché qualcuno può invitare, il numero da solo
    # sarebbe indistinguibile da un canale già a 3 con i presenti.
    direct_legs = {leg: any(p["legs"][leg] for p in profiles) for leg in LEGS}
    # Anche `direct` conta i BIT, non le capacità: se il titolo del canale è il
    # vettore, un secondo numero con un'altra semantica accanto sarebbe illeggibile.
    direct_arbitrary = direct_legs["egress"] and any(
        p.get("egress_scope") == "arbitrary" for p in profiles)
    direct_score = ((1 if tainted else 0) + (1 if direct_legs["private_data"] else 0)
                    + (1 if direct_arbitrary else 0))
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
        # Il vettore così com'è, perché UI e API non lo ricalcolino ognuna a modo
        # suo — è la definizione, non una formattazione.
        "vector": vector,
        "bits": {"tainted": bits[0], "private_data": bits[1],
                 "arbitrary_egress": bits[2]},
        # Perché il secondo bit è spento nonostante i verbi ci siano: un numero
        # che scende senza dire perché è indistinguibile da un difetto di calcolo.
        # `capability_legs` resta la CAPACITÀ, e non mente: quei verbi ci sono.
        "channel_private_data": channel_private_data,
        "private_data_suppressed": bool(channel_private_data is False
                                        and legs["private_data"]),
        "tainted": tainted,
        # Capacità: i verbi presenti, indipendentemente da cosa è accaduto e da
        # come sono confinati.
        "capability": capability,
        "capability_legs": legs,
        # Perché il terzo bit è acceso: un remote non vagliato è un problema di
        # whitelist, un agente con uscita arbitraria è un problema di grant. Si
        # risolvono con azioni diverse e vanno distinti.
        "remote_egress": bool(remote_egress),
        "egress_mode": conf.get("mode", "unknown"),
        "egress_scopes": sorted({pr.get("egress_scope", "none") for pr in closure
                                 if pr.get("legs", {}).get("egress")}),
        # Si calcola sull'OR dei lati come `score`, NON come massimo dei residui
        # per-agente: un agente a 2/3 senza uscita più uno a 1/3 con uscita
        # arbitraria fanno un canale a 3 residuo, mentre il massimo dei residui
        # direbbe 2. La chiusura è l'unità di valutazione, anche qui.
        # Alias storico: `score` ORA è già il rischio residuo, perché conta i bit
        # del vettore e non le capacità. Tenuto per i chiamanti esistenti.
        "residual": score,
        "config_version": cfg.get("version", 0),
    }
