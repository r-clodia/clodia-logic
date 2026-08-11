"""Chi sta guardando quale canale, adesso.

Nato senza protocollo proprio: la webui, con una conversazione aperta, chiama
`/clodia/channels/{tier}/{name}/messages` ogni cinque secondi, e quella chiamata
è già un battito — bastava ascoltarlo. Serviva a una domanda sola: «era davanti
allo schermo quando l'hanno chiamato?».

**Emendato l'11 ago 2026**, quando le domande sono diventate quattro: qui, in
un'altra stanza, con la scheda dietro, via. «Primo piano» e «un'altra scheda»
non sono deducibili da nessuna chiamata — le conosce solo il browser, e se non
le dice non le sa nessuno. Quindi ora c'è un battito ESPLICITO che porta il
fatto invece di tre inferenze che lo approssimano.

Resta la regola che teneva insieme il disegno di prima: **un solo scrittore**.
`beat` scrive, `touch` traduce il polling dei client che non sanno battere (la
PWA). Due scrittori con forme diverse sullo stesso file raccontano storie
diverse appena una rete cade a metà.

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


#: Fin quando un battito vale come presenza. Generoso di proposito: una scheda
#: in secondo piano viene STROZZATA dal browser — i timer scendono a uno al
#: minuto — quindi un TTL stretto trasformerebbe «sta lavorando in un'altra
#: finestra» in «se n'è andato». È l'errore che rende inutile un indicatore: se
#: lampeggia mentre la persona è lì, si smette di guardarlo.
TTL_S = 150.0

#: Chiave del battito «sono nella webui, non importa dove»: serve a distinguere
#: chi è altrove NELLA webui da chi ha la scheda in secondo piano, e entrambi da
#: chi non c'è. Senza, l'unica domanda rispondibile sarebbe «è in questa stanza?»,
#: cioè due stati invece di quattro.
OVUNQUE = "-"


def beat(principal: str, chat: str | None, visible: bool) -> None:
    """Battito esplicito della webui: dove sta guardando e se è in primo piano.

    Il battito è UNO. La tentazione sarebbe dedurre lo stato da ciò che la pagina
    già chiede — e infatti la presenza «in questa stanza» nasce così, dal polling
    dei messaggi. Ma «primo piano» e «un'altra scheda» non sono deducibili da
    nessuna chiamata: solo il browser le conosce, e se non le dice non le sa
    nessuno. Meglio un battito che porta il fatto, che tre inferenze che lo
    approssimano.
    """
    if not principal:
        return
    ora = datetime.now(timezone.utc).isoformat()
    voce = {"ts": ora, "visible": bool(visible)}
    try:
        with _LOCK:
            d = _load()
            d[f"{principal}|{OVUNQUE}"] = voce
            if chat:
                d[f"{principal}|{chat}"] = voce
            _potatura(d)
            _scrivi(d)
    except Exception as e:  # noqa: BLE001
        LOG.debug("battito non registrato (%s): %s", principal, e)


def _potatura(d: dict) -> None:
    if len(d) <= _MAX:
        return
    def _t(v):
        return v.get("ts", "") if isinstance(v, dict) else str(v or "")
    ordinate = sorted(d.items(), key=lambda kv: _t(kv[1]), reverse=True)[:_MAX]
    d.clear()
    d.update(dict(ordinate))


def _scrivi(d: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _quando(v) -> float:
    """Istante di una voce, in entrambe le forme.

    La forma vecchia è una stringa ISO nuda, la nuova un oggetto. Convivono per
    davvero: il file sta nella datadir CONDIVISA, e i due container si aggiornano
    in momenti diversi — per qualche minuto uno scrive la forma nuova e l'altro
    legge. Rifiutare la vecchia significherebbe, in quella finestra, dichiarare
    assenti tutti.
    """
    s = v.get("ts") if isinstance(v, dict) else v
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def stato(principal: str, tier: str, name: str, ora: float | None = None,
          _d: dict | None = None) -> str:
    """`here` | `elsewhere` | `background` | `away`.

    Quattro stati e non due, perché le domande sono diverse: «mi legge adesso?»
    non è «è raggiungibile?». Un pallino solo li fonderebbe, e chi guarda
    dedurrebbe la risposta sbagliata a una delle due.
    """
    import time as _t
    adesso = ora if ora is not None else _t.time()
    d = _d if _d is not None else _load()
    qui = d.get(f"{principal}|{tier}/{name}")
    ovunque = d.get(f"{principal}|{OVUNQUE}")

    def _fresco(v) -> bool:
        return bool(v) and (adesso - _quando(v)) <= TTL_S

    def _visibile(v) -> bool:
        # Forma vecchia (stringa): la visibilità non è mai stata registrata. Si
        # assume di sì, perché quel battito nasceva dal polling di una
        # conversazione APERTA — è ciò che significava allora.
        return v.get("visible", True) if isinstance(v, dict) else True

    if _fresco(qui) and _visibile(qui):
        return "here"
    if _fresco(ovunque):
        return "elsewhere" if _visibile(ovunque) else "background"
    # Una stanza fresca ma non visibile, senza battito generale: è comunque
    # qualcuno con la webui aperta e la scheda dietro.
    if _fresco(qui):
        return "background"
    return "away"


def stati(principals: list[str], tier: str, name: str) -> dict:
    """Gli stati di più persone in UNA lettura del file.

    Una lettura per persona sarebbe stata la via naturale e avrebbe riletto lo
    stesso file dieci volte a ogni polling di ogni scheda aperta — su una lista
    di partecipanti, ogni cinque secondi, per ogni utente collegato.
    """
    if not principals:
        return {}
    import time as _t
    d = _load()
    adesso = _t.time()
    return {p: stato(p, tier, name, ora=adesso, _d=d) for p in principals}


def touch(principal: str, tier: str, name: str) -> None:
    """Battito IMPLICITO: qualcuno sta leggendo i messaggi di questa stanza.

    Resta perché non tutti i client sanno battere: la PWA interroga la
    conversazione e non manda nulla di esplicito. Per quei client «sta leggendo
    questa stanza» è tutto ciò che si può sapere — e assumere che sia in primo
    piano è ciò che quel polling ha sempre significato.

    Un solo posto scrive davvero (`beat`), così le due vie non possono divergere
    sulla forma del file: qui si traduce, non si duplica.
    """
    if not (principal and tier and name):
        return
    beat(principal, f"{tier}/{name}", visible=True)


# ── Rotta del battito ───────────────────────────────────────────────────────
#
# Ha un router PROPRIO e non sta fra le rotte di canale, per una ragione che un
# test ha reso visibile: non è una rotta di scope. Non ha una stanza da
# proteggere e quindi nessuna delle tre guardie (`_require_member`,
# `_require_contributor`, `_require_scope_owner`) — l'identità arriva dal token e
# basta. Messa in mezzo alle altre, il controllo che verifica «ogni rotta di
# canale ha la guardia giusta» le attribuiva la guardia della rotta successiva:
# non un difetto di sicurezza, ma un controllo che smette di dire il vero, che è
# il modo in cui un giorno smetterà di trovare una guardia mancante davvero.

from fastapi import APIRouter, Request  # noqa: E402

router = APIRouter()


@router.post("/api/presence")
async def presence_beat(request: Request) -> dict:
    """Battito della webui: dove sto guardando, e se sono in primo piano.

    L'identità NON viene dal corpo: «sono davide» scritto da chi chiama non è
    una presenza, è un'affermazione. Viene dal token, come ovunque.

    Non fallisce mai in modo rumoroso: un battito perso vale un pallino
    impreciso per un minuto, e non deve poter rompere la pagina che lo manda.
    """
    from .agents import _principal_from_request
    chi = _principal_from_request(request)
    if not chi:
        return {"ok": False, "reason": "anonimo"}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    chat = (body.get("chat") or "").strip() or None
    beat(chi, chat, bool(body.get("visible", True)))
    return {"ok": True}
