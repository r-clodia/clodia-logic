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
