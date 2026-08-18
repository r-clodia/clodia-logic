"""Proxy umano→gateway per l'approvazione dei GATE (M-gate, sostituisce sudo).

La webUI/PWA mostra le richieste di gate pending; l'utente loggato **nel
contesto** approva/nega. L'approvazione conia una capability `ccap1`
(cap=gate:<verb>) — prova crittografica del consenso — e la registra nel
gateway.

Chi ha titolo a decidere dipende da COSA il gate attraversa (voce 23, emendata
dalla 26), non dal verbo in sé:

  - i gate **system** cambiano le regole della macchina e li decide un admin;
  - i gate **walls** e **outward** attraversano il confine di uno scope, e li
    decide l'**owner di quello scope** (voce 24). Un admin non lo sostituisce:
    se lo facesse, l'autorità dell'owner sarebbe decorativa.

Resta il vincolo di sempre: non puoi delegare ciò che non hai.
"""
from __future__ import annotations

import asyncio
import logging
import os

import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..colony import pki
from . import admin, topics_client
from .agents import _principal_from_request

LOG = logging.getLogger("agent-server.api.gate")
router = APIRouter()

_TOKEN_TTL = 120
_HTTP_TIMEOUT = 15


def _post_outcome(chat: str | None, principal: str, text: str) -> None:
    if not chat or not chat.startswith("chan:"):
        return
    parts = chat.split(":")
    if len(parts) < 3:
        return
    try:
        topics_client.post_message(parts[1], parts[2], principal, text, kind="human")
    except Exception as e:  # noqa: BLE001
        LOG.warning("post esito gate in chat %s fallito: %s", chat, e)


def _gw_base() -> str:
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/")
    base = mcp.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return f"{base}/internal/gate"


def _gw(method: str, path: str, principal: str, json: dict | None = None):
    token = pki.mint_session_token("clodia", ttl_seconds=_TOKEN_TTL, principal=principal)
    r = requests.request(method, f"{_gw_base()}{path}",
                         headers={"Authorization": f"Bearer {token}"},
                         json=json, timeout=_HTTP_TIMEOUT)
    return r


def _scope_of(chat: str | None) -> tuple[str, str] | None:
    """`chan:<tier>:<name>:...` → (tier, name). Fuori da una stanza: None."""
    if not chat or not chat.startswith("chan:"):
        return None
    p = chat.split(":")
    return (p[1], p[2]) if len(p) >= 3 else None


def _is_scope_owner(principal: str, scope: tuple[str, str]) -> bool:
    """Fail-closed: se non riusciamo a leggere il topic, NON siamo owner.

    Degradare ad «autorizzato» su un errore di lettura trasformerebbe un guasto
    in un permesso — è il difetto che è costato tre giri il 6 agosto, quando un
    500 del gateway è arrivato travestito da rifiuto di permesso.
    """
    try:
        meta = (topics_client.open_topic(scope[0], scope[1]) or {}).get("meta") or {}
    except Exception as e:  # noqa: BLE001
        LOG.warning("owner di %s/%s illeggibile: %s", scope[0], scope[1], e)
        return False
    return bool(principal) and principal == meta.get("owner")


def _standing(req: dict) -> tuple[str, str, str]:
    """Chi ha titolo su QUESTO gate: `(chi, cosa attraversa, dettaglio)`.

    Esiste per non scrivere due volte la stessa regola. La card deve dire «cosa
    attraversa e chi decide», e la via comoda sarebbe ricalcolarlo nel frontend
    a partire dalla classe: due copie della stessa regola divergono, e quella
    che diverge è sempre la copia che spiega — cioè si finisce a mostrare
    «decide un admin» a un gate che solo l'owner può sbloccare, e a mandare la
    persona sbagliata a cercare un permesso che non ha.

    `chi` è un identificatore stabile (`owner:<tier>/<name>` oppure `admin`),
    non una frase: chi lo legge deve poterlo confrontare, e una frase italiana
    si riformula il giorno dopo.
    """
    klass = (req.get("class") or "").strip().lower()
    scope = _scope_of(req.get("chat"))
    if klass in ("walls", "outward") and scope:
        dove = f"{scope[0]}/{scope[1]}"
        cosa = ("il confine di questa stanza" if klass == "walls"
                else "l'uscita dei dati da questa stanza")
        return f"owner:{dove}", cosa, dove
    if klass == "system":
        return "admin", "le regole della macchina", ""
    # Senza classe o fuori da una stanza: la regola di prima, l'admin. Non è un
    # ripiego silenzioso — la card lo dice, ed è l'unica risposta che non
    # inventa uno scope.
    return "admin", ("le regole della macchina" if klass else
                     "un confine che il gateway non ha classificato"), ""


def _may_decide(principal: str, req: dict) -> tuple[bool, str]:
    """Chi ha titolo a sbloccare QUESTO gate. Ritorna (ok, motivo del rifiuto).

    La regola segue le classi (voce 23, emendata dalla 26): un gate non è una
    proprietà del verbo, è ciò che accade quando un'azione ATTRAVERSA un
    confine. Chi decide è chi ha titolo su QUEL confine.

      system            → un admin. Sono le regole della macchina, non di una
                          stanza: nessuno scope le possiede.
      walls / outward   → l'OWNER dello scope attraversato (voce 24: «è l'owner
                          del gate a sbloccare o negare»).

    Un admin **non sostituisce** l'owner sui gate della sua stanza. Se lo
    facesse, l'autorità dell'owner sarebbe decorativa — e «dichiarato ma nessuno
    lo porta» è il difetto ricorrente di questa settimana, trovato sette volte.
    Un admin resta ovviamente in grado di cambiare il topic dalla porta
    principale: la differenza è che lì la cosa ha un suo nome e un suo log,
    invece di passare per un consenso dato a nome di qualcun altro.

    Fuori da una stanza (job, turno non presidiato) non c'è owner da chiamare in
    causa: decide un admin, come prima.

    Resta il vincolo di sempre — non puoi delegare ciò che non hai. Per i gate
    di sistema è la RBAC; per walls/outward è la proprietà dello scope, che per
    il terzo termine dell'intersezione (voce 29) porta con sé quei verbi nella
    propria stanza.
    """
    verb = (req.get("verb") or "").strip()
    chi, _cosa, dove = _standing(req)

    if chi.startswith("owner:"):
        if _is_scope_owner(principal, tuple(dove.split("/", 1))):
            return True, ""
        return False, (
            f"'{verb}' attraversa il confine di {dove}: "
            f"lo sblocca l'owner di quel topic, non un admin della piattaforma")

    if admin.is_admin(principal):
        return True, ""
    return False, (f"'{principal}' non è autorizzato al verbo '{verb}' "
                   f"(non puoi delegare ciò che non hai)")


class _Unavailable(Exception):
    """Il gateway non risponde. NON è un rifiuto: un guasto travestito da
    decisione manda a chiedere alla persona sbagliata, ed è esattamente il modo
    in cui il 403 su `packs.import_url` è costato tre diagnosi il 6 agosto."""


def _pending_request(principal: str, agent: str, instance: str, verb: str) -> dict | None:
    """La richiesta come la conosce il GATEWAY: `chat` e `class` vengono da lì.

    Prenderle dal body sarebbe chiedere a chi approva in quale stanza si trovava
    l'azione da approvare — la parola di chi chiede su dove si trova.
    """
    try:
        r = _gw("GET", "/pending", principal)
    except Exception as e:  # noqa: BLE001
        raise _Unavailable(str(e)) from e
    if r.status_code >= 500:
        raise _Unavailable(f"HTTP {r.status_code}")
    if r.status_code >= 400:
        return None
    for it in (r.json() or {}).get("requests", []):
        if (it.get("agent") == agent and it.get("verb") == verb
                and (it.get("instance") or "-") == instance):
            return it
    return None


def _standing_error(principal: str, agent: str, instance: str,
                    verb: str) -> JSONResponse | None:
    """`None` = ha titolo a decidere. Altrimenti la risposta da restituire.

    Distingue tre esiti, non due: autorizzato, rifiutato, e **non sappiamo**
    (503). Il terzo esiste perché un guasto che si presenta come rifiuto manda
    a chiedere alla persona sbagliata.
    """
    try:
        req = _pending_request(principal, agent, instance, verb)
    except _Unavailable as e:
        LOG.warning("titolo a decidere non verificabile (%s@%s:%s): %s",
                    agent, instance, verb, e)
        return JSONResponse(
            {"error": "unavailable",
             "detail": "gateway dei gate non raggiungibile: titolo a decidere "
                       "non verificabile, riprova"}, status_code=503)
    if req is None:
        # Richiesta scaduta o mai esistita: senza `chat` autorevole non sappiamo
        # quale confine si attraversa → resta la regola di prima, l'admin.
        req = {"verb": verb, "class": None, "chat": None}
    ok, motivo = _may_decide(principal, req)
    if ok:
        return None
    return JSONResponse({"error": "forbidden", "detail": motivo}, status_code=403)


def _owner_of(scope_key: str, cache: dict) -> str:
    """Nome dell'owner di `tier/name`, letto una volta per stanza.

    La cache è per-richiesta e non globale: con dieci gate nella stessa stanza
    si leggerebbe dieci volte lo stesso topic, ma tenerla fra una richiesta e
    l'altra mostrerebbe come owner chi non lo è più — e la card serve proprio a
    dire a chi rivolgersi.
    """
    if scope_key in cache:
        return cache[scope_key]
    tier, _, name = scope_key.partition("/")
    try:
        meta = (topics_client.open_topic(tier, name) or {}).get("meta") or {}
        chi = str(meta.get("owner") or "")
    except Exception as e:  # noqa: BLE001 — illeggibile → nessun nome, non un nome finto
        LOG.warning("owner di %s illeggibile: %s", scope_key, str(e)[:120])
        chi = ""
    cache[scope_key] = chi
    return chi


def _is_watcher(agent: str) -> bool:
    """Vero se chi chiede è il guardiano della modalità debug.

    Serve a dirlo sulla card. Il 10 ago 2026 Davide ha visto `sysadmin` — che
    non è partecipante di quel canale — chiedere un gate in una sua stanza, e la
    lettura naturale è che qualcosa fosse scappato dal recinto. Era invece il
    watcher, svegliato da un turno fallito: comportamento voluto, e illeggibile.

    Una presenza legittima che sembra un'intrusione costa esattamente quanto
    un'intrusione, finché qualcuno non la spiega.
    """
    try:
        from .. import debug_watch
        return bool(agent) and agent == debug_watch.WATCHER and debug_watch.enabled()
    except Exception:  # noqa: BLE001 — senza il modulo non si etichetta nulla
        return False


def _decorate(req: dict, cache: dict) -> dict:
    """Aggiunge alla richiesta COSA attraversa, CHI decide, e chi sta chiedendo.

    Calcolato qui e non nel frontend: la regola del titolo vive in `_standing`,
    e una seconda copia che serve solo a spiegare diverge da quella che decide.
    """
    chi, cosa, dove = _standing(req)
    fuori = {**req, "crosses": cosa, "decided_by": chi}
    if _is_watcher(req.get("agent") or ""):
        fuori["asker_role"] = "debug-watcher"
        fuori["asker_note"] = (
            "guardiano della modalità debug: non è un partecipante del canale, "
            "è entrato perché un turno è fallito lì")
    if chi.startswith("owner:"):
        nome = _owner_of(dove, cache)
        fuori["decider_name"] = nome
        fuori["scope"] = dove
    return fuori


@router.get("/api/gate/pending")
async def pending(request: Request):
    principal = _principal_from_request(request)
    if not principal:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    r = _gw("GET", "/pending", principal)
    if r.status_code >= 400:
        return JSONResponse({"requests": []})
    corpo = r.json() or {}
    cache: dict = {}
    corpo["requests"] = [await asyncio.to_thread(_decorate, q, cache)
                         for q in (corpo.get("requests") or [])]
    return JSONResponse(corpo, status_code=r.status_code)


@router.post("/api/gate/approve")
async def approve(request: Request):
    """Approva un gate: consente a (agent, instance) l'uso di `verb`."""
    principal = _principal_from_request(request)
    if not principal:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    agent = (body.get("agent") or "").strip()
    instance = (body.get("instance") or "-").strip() or "-"
    verb = (body.get("verb") or "").strip()
    minutes = body.get("minutes", 10)
    # Fin dove vale questo sì: solo adesso, sempre in questa stanza, ovunque.
    ricorda = (body.get("remember") or "once").strip().lower()
    if not (agent and verb):
        return JSONResponse({"error": "agent/verb richiesti"}, status_code=400)
    if ricorda not in ("once", "topic", "global"):
        return JSONResponse({"error": f"remember sconosciuto: {ricorda}"},
                            status_code=400)
    rifiuto = await asyncio.to_thread(_standing_error, principal, agent, instance, verb)
    if rifiuto is not None:
        return rifiuto
    # SECONDO titolo, e più stretto del primo: chi possiede la stanza decide per
    # la stanza; per l'istanza intera decide chi possiede l'istanza.
    #
    # Non è pignoleria. «Approva ovunque» scritto nella lista globale vale per
    # OGNI stanza, comprese quelle di cui chi approva non sa nulla — e un owner
    # che aprisse una destinazione per tutti starebbe decidendo al posto di
    # altri owner. Chiedere qui l'admin è ciò che tiene separato «la mia stanza»
    # da «la macchina», la stessa separazione che regge le classi dei gate.
    if ricorda == "global" and not admin.is_admin(principal):
        return JSONResponse(
            {"error": "forbidden",
             "detail": "«approva ovunque» scrive nella lista dell'ISTANZA: vale "
                       "anche nelle stanze che non sono tue, quindi lo decide un "
                       "admin della piattaforma. Puoi approvare per questa stanza "
                       "o solo per stavolta."}, status_code=403)
    try:
        cap = pki.mint_capability(agent, instance, minutes, by=principal,
                                  cap=f"gate:{verb}")
    except Exception as e:  # noqa: BLE001
        LOG.error("mint_capability(gate) fallito per %s@%s:%s: %s", agent, instance, verb, e)
        return JSONResponse({"error": "mint_failed", "detail": str(e)}, status_code=500)
    r = _gw("POST", "/grant", principal,
            {"agent": agent, "instance": instance, "verb": verb, "token": cap["token"]})
    LOG.info("gate approve %s@%s:%s da %s (jti=%s, remember=%s) → %s", agent,
             instance, verb, principal, cap.get("jti"), ricorda, r.status_code)
    memoria = None
    if r.status_code == 200 and ricorda != "once":
        # La capability è già stata concessa: la chiamata in attesa procede
        # comunque, e questo passo riguarda solo le VOLTE SUCCESSIVE. In
        # quest'ordine perché se scrivere la lista fallisce, l'agente non resta
        # bloccato per un difetto della memoria — l'approvazione era valida.
        # La stanza la dice il GATEWAY, non il corpo della richiesta: chiederla a
        # chi approva significherebbe fidarsi della sua parola su dove si trovava
        # l'azione da approvare — e qui quella parola diventa una voce in una
        # whitelist permanente.
        try:
            _req = _pending_request(principal, agent, instance, verb) or {}
        except _Unavailable:
            _req = {}
        scope = _scope_of(_req.get("chat"))
        dove = f"{scope[0]}/{scope[1]}" if (ricorda == "topic" and scope) else ""
        if ricorda == "topic" and not dove:
            memoria = {"remembered": False,
                       "error": "non so in quale stanza ricordarlo: la richiesta "
                                "non dichiara un canale"}
        else:
            rr = _gw("POST", "/allow", principal,
                     {"verb": verb, "direction": "egress", "scope": dove})
            # `remembered` si scrive SEMPRE, anche in caso di errore. Lasciarlo
            # assente farebbe funzionare i controlli — una chiave mancante è
            # falsa — al prezzo di un corpo in cui «non ricordato» è
            # un'informazione implicita: chi legge la risposta non trova la
            # differenza fra «non è riuscito» e «non è stato chiesto».
            try:
                memoria = rr.json()
            except Exception:  # noqa: BLE001
                memoria = {"error": rr.text[:200]}
            if rr.status_code != 200 or "remembered" not in memoria:
                memoria = {"remembered": False,
                           "error": str(memoria.get("error") or f"HTTP {rr.status_code}")[:200]}
    if r.status_code == 200:
        quanto = {"once": "per stavolta",
                  "topic": "e ricordato per questa stanza",
                  "global": "e ricordato per tutta l'istanza"}[ricorda]
        if memoria and not memoria.get("remembered"):
            # Dirlo: un'approvazione che si crede permanente e non lo è
            # ricompare domani, e chi la rivede pensa che il gate sia rotto.
            quanto = f"per stavolta (non ricordato: {memoria.get('error', '?')})"
        await asyncio.to_thread(_post_outcome, body.get("chat"), principal,
                      f"🔓 @{agent}: approvato l'uso di {verb} — {quanto}")
    corpo = r.json() if r.status_code == 200 else {"error": r.text[:200]}
    if memoria is not None:
        corpo["memory"] = memoria
    return JSONResponse(corpo, status_code=r.status_code)


@router.post("/api/gate/deny")
async def deny(request: Request):
    principal = _principal_from_request(request)
    if not principal:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    agent = (body.get("agent") or "").strip()
    verb = (body.get("verb") or "").strip()
    instance = (body.get("instance") or "-").strip() or "-"
    # Anche negare è una decisione sulla richiesta di qualcun altro. Finora non
    # aveva alcun controllo: chiunque autenticato poteva negare il gate di
    # chiunque — non una fuga di dati, ma il modo più economico per fermare il
    # lavoro altrui. Stesso titolo dell'approvazione.
    rifiuto = await asyncio.to_thread(_standing_error, principal, agent, instance, verb)
    if rifiuto is not None:
        return rifiuto
    r = _gw("POST", "/deny", principal,
            {"agent": agent, "instance": instance, "verb": verb})
    if r.status_code == 200:
        await asyncio.to_thread(_post_outcome, body.get("chat"), principal,
                                f"⛔ @{agent}: negato l'uso di {verb}")
    return JSONResponse(r.json(), status_code=r.status_code)
