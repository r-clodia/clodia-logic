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
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ..config import data_path, workspace_path

LOG = logging.getLogger("agent-server.agents.seed_sync")

PACK_AGENTS_DIR = workspace_path("catalogs/packs/base-pack/agents")
DATA_AGENTS_DIR = data_path("agents")


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
