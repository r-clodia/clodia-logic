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
    klass = (req.get("class") or "").strip().lower()
    scope = _scope_of(req.get("chat"))

    if klass in ("walls", "outward") and scope:
        if _is_scope_owner(principal, scope):
            return True, ""
        return False, (
            f"'{verb}' attraversa il confine di {scope[0]}/{scope[1]}: "
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


@router.get("/api/gate/pending")
async def pending(request: Request):
    principal = _principal_from_request(request)
    if not principal:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    r = _gw("GET", "/pending", principal)
    if r.status_code >= 400:
        return JSONResponse({"requests": []})
    return JSONResponse(r.json(), status_code=r.status_code)


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
    if not (agent and verb):
        return JSONResponse({"error": "agent/verb richiesti"}, status_code=400)
    rifiuto = _standing_error(principal, agent, instance, verb)
    if rifiuto is not None:
        return rifiuto
    try:
        cap = pki.mint_capability(agent, instance, minutes, by=principal,
                                  cap=f"gate:{verb}")
    except Exception as e:  # noqa: BLE001
        LOG.error("mint_capability(gate) fallito per %s@%s:%s: %s", agent, instance, verb, e)
        return JSONResponse({"error": "mint_failed", "detail": str(e)}, status_code=500)
    r = _gw("POST", "/grant", principal,
            {"agent": agent, "instance": instance, "verb": verb, "token": cap["token"]})
    LOG.info("gate approve %s@%s:%s da %s (jti=%s) → %s", agent, instance, verb,
             principal, cap.get("jti"), r.status_code)
    if r.status_code == 200:
        _post_outcome(body.get("chat"), principal, f"🔓 @{agent}: approvato l'uso di {verb} — l'agente procede")
    return JSONResponse(r.json(), status_code=r.status_code)


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
    rifiuto = _standing_error(principal, agent, instance, verb)
    if rifiuto is not None:
        return rifiuto
    r = _gw("POST", "/deny", principal,
            {"agent": agent, "instance": instance, "verb": verb})
    if r.status_code == 200:
        _post_outcome(body.get("chat"), principal, f"⛔ @{agent}: negato l'uso di {verb}")
    return JSONResponse(r.json(), status_code=r.status_code)
