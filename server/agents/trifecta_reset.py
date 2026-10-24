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


def toolbox_signature(meta: Optional[dict]) -> str:
    """Firma della toolbox della stanza (#605), `''` se la stanza non ne ha.

    Da #606 l'uscita di un canale non dipende più solo da CHI c'è: un verbo di
    uscita può arrivare dalla toolbox della stanza, che l'owner cambia senza
    toccare la composizione. Senza questa firma si approverebbe una stanza, si
    abiliterebbe `email.*` per tutti i partecipanti e si terrebbe lo zero.

    Vuota quando la toolbox è vuota: così al deploy non decadono in blocco le
    baseline già firmate: decade solo ciò che una toolbox ce l'ha davvero.
    """
    raw = (meta or {}).get("tools")
    raw = raw if isinstance(raw, dict) else {}
    # Normalizzazione e liste vuote come il gateway (`room_tools.toolbox`): due
    # scritture equivalenti non devono far decadere un reset.
    norm = lambda x: " ".join(str(x).split())  # noqa: E731
    enabled = sorted({norm(x) for x in (raw.get("enabled") or [])
                      if norm(x)}) if isinstance(raw.get("enabled"), list) else []
    exc_raw = raw.get("except") if isinstance(raw.get("except"), dict) else {}
    exc = sorted(f"{a}:{','.join(v)}" for a, v in (
        (a, sorted({norm(x) for x in lst if norm(x)}))
        for a, lst in exc_raw.items() if isinstance(lst, list)) if v)
    if not enabled and not exc:
        return ""
    return hashlib.sha256("|".join([*enabled, "#", *exc]).encode()).hexdigest()[:8]


def composition_epoch(participants: Iterable[str],
                      meta: Optional[dict] = None) -> str:
    """Firma breve della composizione — stessa forma del gate di contesto.

    Duplicata dal gateway di proposito: qui non si importa `clodia-tools`, e una
    dipendenza fra i due processi per otto caratteri di hash costerebbe più della
    riga in doppio. Se le due divergessero, il reset decadrebbe più spesso del
    necessario — un errore nella direzione prudente.

    `meta` aggiunge la toolbox della stanza (#606): la baseline è «questa
    composizione CON questi verbi», perché da #605 i due non cambiano più
    insieme.
    """
    nomi = sorted({str(x).strip() for x in (participants or []) if str(x).strip()})
    firma = toolbox_signature(meta)
    chiave = "|".join(nomi) + (f"#{firma}" if firma else "")
    return hashlib.sha256(chiave.encode()).hexdigest()[:8]


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


def data_signature(paths: Iterable[str]) -> str:
    """Firma dei dati riservati presenti in un canale.

    Serve a distinguere «i dati che l'owner ha approvato» da «dati arrivati dopo».
    Si firmano i path (con la dimensione, quando la si conosce): un file
    sostituito con contenuto nuovo allo stesso path è un dato nuovo, e senza la
    dimensione passerebbe per lo stesso.
    """
    voci = sorted(str(x) for x in (paths or []))
    return hashlib.sha256("|".join(voci).encode()).hexdigest()[:12]


def set_reset(tier: str, name: str, by: str, participants: Iterable[str],
              data_paths: Iterable[str] | None = None,
              meta: Optional[dict] = None) -> dict:
    """Registra la BASELINE: «questo stato lo approvo io».

    Non è un silenziamento — è il punto da cui l'analisi RIPARTE:

        «il reset approva lo stato corrente come sicuro e da lì si riparte a
         misurare le contaminazioni ed i rischi» (Davide, 17 ago 2026)

    Tre rischi, tre modi di ripartire:

    1. **fonte non censita** — il taint viene azzerato al momento del reset e si
       riaccende da sé al primo ingresso successivo. Il meccanismo esiste già
       (`taint.clear` + `taint.mark`), qui si usa;
    2. **dati riservati nel canale** — si firma ciò che c'è ORA: dopo, il bit si
       accende solo per ciò che non era nella firma;
    3. **esfiltrazione su egress non censiti** — è una capacità dei presenti
       E dei verbi che la stanza abilita (#605), quindi la baseline è la
       composizione PIÙ la toolbox: cambiare una delle due fa decadere il reset.
    """
    voce = {
        "by": by,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "epoch": composition_epoch(participants, meta),
        # I dati riservati APPROVATI: sia la firma (per il confronto veloce) sia
        # l'elenco, perché «cosa è arrivato dopo» è la domanda che si porrà chi
        # vede il bit riaccendersi, e una firma da sola non la risponde.
        "data_sig": data_signature(data_paths or []),
        "data_paths": sorted(str(x) for x in (data_paths or [])),
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


def new_private_data(voce: dict, paths: Iterable[str]) -> list[str]:
    """Dati riservati presenti ORA che NON erano nella baseline approvata.

    Vuoto = niente di nuovo dal reset, e il secondo bit resta spento. Non vuoto =
    il bit si riaccende, e questo elenco dice per cosa — «il canale è tornato a
    rischio» non è azionabile, «è arrivato contratto.pdf» sì.
    """
    approvati = set(voce.get("data_paths") or [])
    return sorted(p for p in (str(x) for x in (paths or [])) if p not in approvati)


def active(tier: str, name: str, participants: Iterable[str],
           meta: Optional[dict] = None) -> Optional[dict]:
    """La voce di reset, se c'è ED è ancora valida per questa composizione.

    Una composizione cambiata non produce un errore: il reset semplicemente non
    combacia più, e il punteggio torna a parlare da sé. Da #606 vale lo stesso
    per la toolbox della stanza, che muove le stesse capacità.
    """
    voce = _load().get(scope_key(tier, name))
    if not voce:
        return None
    atteso = composition_epoch(participants, meta)
    if voce.get("epoch") != atteso:
        LOG.info("trifecta reset su %s/%s DECADUTO: composizione o toolbox "
                 "cambiate (%s → %s)", tier, name, voce.get("epoch"), atteso)
        return None
    return voce
