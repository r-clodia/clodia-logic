"""«Reset trifecta»: l'owner dichiara di rispondere lui di questo canale.

Richiesta dell'owner, 17 ago 2026:

    «le tre scimmiette trifecta dovrebbero avere un bottoncino 'reset trifecta'
    che riporta a 0/3 sotto la responsabilità dell'owner»

Serve perché nessuna euristica indovina tutti i casi. La regola sui dati del
canale copre il caso misurato — un canale di soli working file — ma un punteggio
che non si può mai contraddire diventa un semaforo da ignorare, e un semaforo
ignorato è peggio di nessun semaforo.

Tre proprietà, e ognuna esiste per un motivo preciso.

**Non è un silenziamento, è una firma.** Si registra CHI e QUANDO, e il payload
del canale lo dice: `reset_by`/`reset_at` viaggiano accanto al punteggio. Un
azzeramento anonimo sarebbe indistinguibile da un difetto di calcolo, che è
esattamente il modo in cui questa misura ha già perso credibilità una volta.

**Decade se cambia la composizione.** La firma è legata a `composition_epoch`
(la stessa che invalida gli unlock del gate di contesto, #77): aggiungere un
partecipante produce un'epoca diversa e il reset non combacia più. Senza questo,
si azzererebbe un canale di tre agenti e poi ci si aggiungerebbe chi ha uscita
arbitraria, tenendosi lo zero.

**Non spegne il primo bit per sempre.** `tainted` è un EVENTO: il reset lo
azzera adesso (come fa già l'approvazione di un gate di contesto), e il bit si
riaccende al primo ingresso di contenuto non vagliato. Un reset che rendesse un
canale permanentemente pulito sarebbe una bugia con la firma dell'owner sopra.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

LOG = logging.getLogger("agent-server.agents.trifecta_reset")


def _path() -> Path:
    base = os.environ.get("CLODIA_DATA", "/datadir")
    return Path(base) / "agent-state" / "trifecta-reset.json"


def composition_epoch(participants: Iterable[str]) -> str:
    """Firma breve della composizione — stessa forma del gate di contesto.

    Duplicata dal gateway di proposito: qui non si importa `clodia-tools`, e una
    dipendenza fra i due processi per otto caratteri di hash costerebbe più della
    riga in doppio. Se le due divergessero, il reset decadrebbe più spesso del
    necessario — un errore nella direzione prudente.
    """
    nomi = sorted({str(x).strip() for x in (participants or []) if str(x).strip()})
    return hashlib.sha256("|".join(nomi).encode()).hexdigest()[:8]


def _load() -> dict:
    p = _path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError) as e:
        LOG.warning("trifecta-reset illeggibile (%s): si considera assente", type(e).__name__)
        return {}


def _save(d: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def scope_key(tier: str, name: str) -> str:
    return f"{tier}/{name}"


def set_reset(tier: str, name: str, by: str, participants: Iterable[str]) -> dict:
    """Registra il reset. Ritorna la voce salvata."""
    voce = {
        "by": by,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "epoch": composition_epoch(participants),
    }
    d = _load()
    d[scope_key(tier, name)] = voce
    _save(d)
    LOG.info("trifecta reset su %s/%s da %s (epoca %s)", tier, name, by, voce["epoch"])
    return voce


def clear_reset(tier: str, name: str) -> bool:
    """Revoca il reset. `False` se non c'era."""
    d = _load()
    if d.pop(scope_key(tier, name), None) is None:
        return False
    _save(d)
    LOG.info("trifecta reset revocato su %s/%s", tier, name)
    return True


def active(tier: str, name: str, participants: Iterable[str]) -> Optional[dict]:
    """La voce di reset, se c'è ED è ancora valida per questa composizione.

    Una composizione cambiata non produce un errore: il reset semplicemente non
    combacia più, e il punteggio torna a parlare da sé.
    """
    voce = _load().get(scope_key(tier, name))
    if not voce:
        return None
    atteso = composition_epoch(participants)
    if voce.get("epoch") != atteso:
        LOG.info("trifecta reset su %s/%s DECADUTO: la composizione è cambiata "
                 "(%s → %s)", tier, name, voce.get("epoch"), atteso)
        return None
    return voce
