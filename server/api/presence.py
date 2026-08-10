"""Chi sta guardando quale canale, adesso.

Non serve un protocollo nuovo. La webui, con una conversazione aperta, chiama
`/clodia/channels/{tier}/{name}/messages` **ogni cinque secondi**, e quella
chiamata è autenticata: porta con sé chi è e quale stanza sta guardando. È già
un battito — mancava solo qualcuno che lo ascoltasse.

Costruire un canale dedicato (websocket, long-poll, un `ping` esplicito)
avrebbe aggiunto una seconda fonte di verità sulla stessa cosa, e due fonti
sulla stessa cosa divergono: la pagina aperta e il ping possono raccontare
storie diverse quando una rete cade a metà.

**A cosa serve.** A non mandare su Telegram una menzione a chi era davanti allo
schermo quando è arrivata. La notifica è per chi non c'era.

**Dove sta il file.** Nella datadir condivisa, perché a scriverlo è
l'agent-server (che serve la UI) e a leggerlo è il gateway (che recapita). È lo
stesso posto e lo stesso motivo della coda delle notifiche.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

LOG = logging.getLogger("agent-server.api.presence")

_LOCK = Lock()
#: Quante voci teniamo. Una per (persona, stanza) aperta: poche decine anche in
#: un'istanza affollata. Il tetto esiste perché un file che cresce senza limite
#: è un modo lento di riempire un disco.
_MAX = 500


def _path() -> Path:
    base = os.environ.get("CLODIA_DATA", "/datadir")
    return Path(base) / "presence.json"


def _key(principal: str, tier: str, name: str) -> str:
    return f"{principal}|{tier}/{name}"


def _load() -> dict:
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def touch(principal: str, tier: str, name: str) -> None:
    """Registra che `principal` sta guardando `tier/name` adesso.

    Best-effort in ogni senso: un errore qui non deve mai far fallire la
    lettura di un canale. Al peggio si manda una notifica a chi era presente,
    che è fastidioso; far fallire l'apertura della chat sarebbe peggio.
    """
    if not principal or not tier or not name:
        return
    try:
        with _LOCK:
            d = _load()
            d[_key(principal, tier, name)] = datetime.now(timezone.utc).isoformat()
            if len(d) > _MAX:
                # Si tengono le più recenti: una presenza vecchia non è una
                # presenza, e tenerla farebbe saltare notifiche a chi è uscito.
                d = dict(sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:_MAX])
            p = _path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
    except Exception as e:  # noqa: BLE001
        LOG.debug("presence non registrata (%s): %s", principal, e)
