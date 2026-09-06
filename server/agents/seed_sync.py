"""Auto-sync dei SEED dal base-pack bundled alla datadir.

La quarta sincronizzazione, e mancava. Skill, rule e costituzioni arrivano dal
pack a ogni avvio — `skill_sync`, `rule_sync`, `constitution_sync` — **i seed
no**: un seed aggiunto al base-pack non compariva mai su un'istanza già
esistente, perché la datadir viene popolata solo alla nascita.

Si è visto l'8 ago 2026 con l'arciseed: aggiunto al pack, mergiato, deployato, e
sull'istanza non c'era. Il gateway ha continuato a usare il pavimento di
bootstrap e l'ha detto nel log — che è l'unica ragione per cui ce ne siamo
accorti.

**Cosa fa e cosa NON fa.** Copia i seed del pack che nella datadir **non
esistono**. Non tocca quelli che ci sono già: un seed materializzato può essere
stato modificato dall'owner — verbi, provider, prompt — e sovrascriverlo
significherebbe cancellare una decisione presa. È la stessa direzione delle altre
tre sync sul catalogo dati: la copia locale vince.

Quindi questo NON chiude l'aggiornamento dei seed esistenti, che resta il resto
della #25: sapere quando una versione nuova del pack debba prevalere su una
modifica locale è una domanda di prodotto, non di codice.

**Il caso che invece si può decidere: un campo NUOVO.** Un campo che nella copia
locale non esiste affatto non è una modifica dell'owner — è un campo che non
esisteva quando quella copia è stata fatta. Sovrascriverlo non cancella nessuna
decisione, perché non c'è nessuna decisione da cancellare.

Serviva subito, il 12 ago: `native_tools` è arrivato nel pack e sull'istanza i
seed non l'avevano, quindi la restrizione degli strumenti nativi era **inerte** —
`None` da tutte le parti, nessuno strumento negato. La direzione d'errore giusta
(non si chiude per sbaglio), ma una funzione di sicurezza che non fa niente e non
lo dice è peggio di una assente.

Il backfill copia SOLO le chiavi elencate in `BACKFILL_FIELDS`, e solo quando la
copia locale non le ha. Una chiave presente col valore vuoto — `native_tools: []`
— è una dichiarazione, e non si tocca.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..config import data_path, workspace_path

LOG = logging.getLogger("agent-server.agents.seed_sync")

PACK_AGENTS_DIR = workspace_path("catalogs/packs/base-pack/agents")
DATA_AGENTS_DIR = data_path("agents")

#: Campi che si riempiono nella copia locale quando lì NON esistono. Elenco
#: chiuso di proposito: è la differenza fra «riempire un campo nuovo» e
#: «aggiornare un seed», che resta la domanda aperta della #25.
#:
#: Tutti e tre sono campi che RESTRINGONO o dichiarano un vincolo, e non è un
#: caso: sono quelli in cui l'assenza nella copia locale significa «questo seed è
#: stato copiato prima che il vincolo esistesse», mai «l'owner ha deciso di non
#: averlo». Un campo che ALLARGA non entrerebbe in questa lista con la stessa
#: leggerezza — riempirlo darebbe a un agente un potere che nessuno gli ha dato
#: su quell'istanza.
BACKFILL_FIELDS: tuple[str, ...] = ("native_tools", "denied_tools", "all_tier")


def sync_seeds() -> list[str]:
    """Materializza i seed del pack assenti dalla datadir. Ritorna i nomi copiati."""
    src = Path(PACK_AGENTS_DIR)
    dst = Path(DATA_AGENTS_DIR)
    if not src.is_dir():
        LOG.warning("base-pack senza cartella agents (%s): nessun seed da sincronizzare", src)
        return []
    copiati: list[str] = []
    try:
        dst.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        LOG.warning("datadir agents non creabile (%s): sync saltata", e)
        return []
    for d in sorted(src.iterdir()):
        if not d.is_dir() or not (d / "agent.yaml").is_file():
            continue
        target = dst / d.name
        if target.exists():
            continue          # già materializzato: non si sovrascrive (vedi docstring)
        try:
            shutil.copytree(d, target)
            copiati.append(d.name)
            LOG.info("seed '%s' materializzato dal base-pack", d.name)
        except Exception as e:  # noqa: BLE001 — un seed che non si copia non
            # deve impedire agli altri di arrivare, né bloccare il boot
            LOG.warning("seed '%s' non materializzato: %s", d.name, e)
    return copiati


def backfill_new_fields() -> dict:
    """Riempie nei seed della datadir i campi NUOVI del pack. Ritorna cosa ha fatto.

    Solo le chiavi di `BACKFILL_FIELDS`, solo se ASSENTI nella copia locale. Una
    chiave presente non viene toccata nemmeno se il valore è vuoto: `[]` è una
    dichiarazione dell'owner, `None` no — e la differenza fra le due è tutta la
    ragione per cui questa funzione può esistere senza cancellare niente.
    """
    src = Path(PACK_AGENTS_DIR)
    dst = Path(DATA_AGENTS_DIR)
    fatto: dict = {}
    if not src.is_dir() or not dst.is_dir():
        return fatto
    for d in sorted(src.iterdir()):
        pack_y = d / "agent.yaml"
        loc_y = dst / d.name / "agent.yaml"
        if not (d.is_dir() and pack_y.is_file() and loc_y.is_file()):
            continue
        try:
            dal_pack = yaml.safe_load(pack_y.read_text(encoding="utf-8")) or {}
            locale = yaml.safe_load(loc_y.read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001 — un seed illeggibile non ferma gli altri
            LOG.warning("backfill '%s': agent.yaml illeggibile (%s)", d.name, e)
            continue
        aggiunti = [k for k in BACKFILL_FIELDS
                    if k in dal_pack and k not in locale]
        if not aggiunti:
            continue
        for k in aggiunti:
            locale[k] = dal_pack[k]
        try:
            # Riscrittura completa del file: perde i COMMENTI della copia locale,
            # e va detto. Il seed nella datadir è una copia operativa — la versione
            # commentata è quella del pack, in git, che è dove si legge il perché.
            loc_y.write_text(yaml.safe_dump(locale, allow_unicode=True,
                                            sort_keys=False), encoding="utf-8")
            fatto[d.name] = aggiunti
            LOG.info("backfill seed '%s': %s", d.name, ", ".join(aggiunti))
        except Exception as e:  # noqa: BLE001
            LOG.warning("backfill '%s' non scritto: %s", d.name, e)
    return fatto


# ── Drift: il seed installato è ancora quello del pack? ──────────────────────

#: Quanto di un valore si mostra prima di troncarlo. Un `tool_permissions` di 40
#: voci stampato per intero non è un'informazione, è una pagina da saltare.
_MAX_VALORE = 120


def _breve(v: Any) -> str:
    s = str(v)
    return s if len(s) <= _MAX_VALORE else s[: _MAX_VALORE - 1] + "…"


def _delta(campo: str, dal_pack: Any, locale: Any) -> dict | None:
    """La differenza fra due valori dello stesso campo, o None se non c'è.

    Le liste si confrontano per CONTENUTO: `native_tools` riordinato non cambia
    niente di ciò che l'agente può fare, e un rilevatore che segnala un riordino
    è un rilevatore che nessuno rilegge. Quando invece divergono, ciò che serve
    non è il valore intero — sono le voci entrate e uscite: su `gated_tools` con
    19 verbi la riga utile è «manca `gsheets.write_range`», non l'elenco.
    """
    if dal_pack == locale:
        return None
    if isinstance(dal_pack, list) and isinstance(locale, list):
        tolti = [x for x in dal_pack if x not in locale]
        aggiunti = [x for x in locale if x not in dal_pack]
        if not tolti and not aggiunti:
            return None
        return {"field": campo,
                "removed": [_breve(x) for x in tolti],
                "added": [_breve(x) for x in aggiunti],
                "pack": _breve(dal_pack), "local": _breve(locale)}
    return {"field": campo, "removed": [], "added": [],
            "pack": _breve(dal_pack), "local": _breve(locale)}


def seed_drift(pack_seed_dirs: Iterable[Path | str],
               data_agents_dir: Path | str) -> list[dict]:
    """Divergenze fra i seed DICHIARATI dal pack e le copie INSTALLATE.

    La domanda che nessuno poteva fare (clodia-platform#266, punto 4 della
    #211). `_dichiarazioni_inerti` (loader) guarda dentro il file locale e vede
    ciò che è **spento**: una riga commentata. Un campo cancellato del tutto non
    lascia niente da guardare — nessun commento, nessun errore di parse — e per
    quel rilevatore il seed è pulito. Poi arriva un update del pack, il campo
    torna, e il comportamento di un agente cambia per una ragione che nessuno
    collega all'aggiornamento.

    Il confronto è sul PARSATO, non sul testo: i seed dei pack sono scritti con
    pagine di prosa commentata, e un diff testuale sarebbe rumore. Effetto
    collaterale utile: prende anche la forma che la regex della #343 non prende
    (`#   gated_tools:`, chiave di lista senza valore sulla riga), perché non
    guarda com'è fatto il commento ma cosa resta nella dichiarazione.

    SEGNALA e basta, come tutto il resto di questa famiglia: ripristinare
    d'ufficio significherebbe riaccendere per conto di nessuno campi che
    qualcuno ha spento per una ragione scritta da nessuna parte — e su
    `gated_tools` decidere da sé chi deve dare un consenso.

    `pack_seed_dirs` sono le directory seed del pack (`agents/<n>/`), che il
    chiamante risolve: bundled o scaricate dall'upstream. Ritorna una voce per
    ogni seed che ha qualcosa da dire — nessuna voce = nessuna divergenza.
    """
    dst = Path(data_agents_dir)
    fuori: list[dict] = []
    for d in pack_seed_dirs:
        d = Path(d)
        pack_y = d / "agent.yaml"
        if not d.is_dir() or not pack_y.is_file():
            continue
        voce: dict = {"name": d.name, "installed": True, "missing": [],
                      "changed": [], "extra": [], "error": ""}
        loc_y = dst / d.name / "agent.yaml"
        if not loc_y.is_file():
            # Il pack lo dichiara e sull'istanza non c'è: `sync_seeds` lo
            # materializza al boot, quindi qui è raro — ed è comunque una
            # divergenza fra dichiarato e installato, che è ciò che si sta
            # misurando.
            voce["installed"] = False
            fuori.append(voce)
            continue
        try:
            dal_pack = yaml.safe_load(pack_y.read_text(encoding="utf-8")) or {}
            locale = yaml.safe_load(loc_y.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            # Un seed illeggibile non ferma gli altri: il drift di un pack è
            # utile anche quando un file solo è rotto — e quel file rotto è a
            # sua volta una cosa da vedere.
            voce["error"] = f"agent.yaml illeggibile: {str(e)[:120]}"
            fuori.append(voce)
            continue
        if not isinstance(dal_pack, dict) or not isinstance(locale, dict):
            voce["error"] = "agent.yaml non è una mappa di campi"
            fuori.append(voce)
            continue
        voce["missing"] = [k for k in dal_pack if k not in locale]
        voce["extra"] = [k for k in locale if k not in dal_pack]
        for k, v in dal_pack.items():
            if k not in locale:
                continue
            diff = _delta(k, v, locale[k])
            if diff:
                voce["changed"].append(diff)
        if voce["missing"] or voce["extra"] or voce["changed"]:
            fuori.append(voce)
    return fuori
