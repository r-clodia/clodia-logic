"""Client verso gli endpoint interni dei topic del gateway (Topic System v2).

Stesso pattern di provider_store/imagegen_client: il runner di clodia-logic fa da
proxy per la webui, chiamando il gateway (`/internal/topics`) con un token ckt1
firmato per il principal `clodia`. I topic v2 vivono dietro il gateway; qui li
leggiamo solo per servirli alla pagina Topics.
"""
from __future__ import annotations

import os
import re
import asyncio
from urllib.parse import quote

import requests

from ..colony import pki
from .gateway_http import GatewayHTTP

_PRINCIPAL = os.environ.get("CLODIA_PROVIDER_PRINCIPAL", "clodia")
_TOKEN_TTL = 300
# Due budget, perché dietro lo stesso gateway ci sono due mondi. Il control
# plane (aprire un topic, leggere i messaggi, i permessi) è una chiamata dentro
# la rete docker: 15s erano il tempo di un'API pubblica, e ogni attesa è un
# thread fermo. Le operazioni che attraversano un sistema ESTERNO al gateway —
# storage remoto tipo Drive, l'API di Telegram, i byte di un file — tengono il
# budget lungo: lì la lentezza è del sistema a valle, non un guasto da troncare.
_HTTP_TIMEOUT = 5
_HTTP_TIMEOUT_ESTERNO = 30

_gw_http = GatewayHTTP("topics")


async def _to_thread(func, /, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


class TopicsConflictError(RuntimeError):
    """Optimistic lock perso. Distinta da TopicsClientError perché la risposta
    giusta è diversa: rileggere e rifondere, non ritentare uguale."""


class TopicsClientError(RuntimeError):
    """Errore parlando col gateway dei topic.

    Porta `status` e `detail` quando nasce da una risposta HTTP, perché la classe
    dell'errore va conservata fino alla UI: un rifiuto per validazione (4xx) con un
    messaggio azionabile non deve arrivare all'utente come un 502, cioè come un
    guasto del server. È la stessa lezione del 424 sullo storage non raggiungibile
    (#115): un rifiuto che sembra un crash manda a cercare il problema nel posto
    sbagliato.
    """

    def __init__(self, message: str, status: int | None = None,
                 detail: str | None = None):
        super().__init__(message)
        self.status = status
        self.detail = detail or message

    @property
    def is_client_error(self) -> bool:
        return bool(self.status and 400 <= self.status < 500)


def _http_error(what: str, r) -> TopicsClientError:
    """Costruisce l'errore da una risposta non-200, estraendo il messaggio del
    gateway invece di annidare il suo JSON in una stringa."""
    detail = ""
    try:
        body = r.json()
        detail = str(body.get("error") or body.get("detail") or "").strip()
    except Exception:  # noqa: BLE001 — corpo non JSON
        detail = (r.text or "")[:400].strip()
    return TopicsClientError(f"gateway {what} → HTTP {r.status_code}: {detail[:400]}",
                             status=r.status_code, detail=detail or f"HTTP {r.status_code}")


def _base() -> str:
    explicit = os.environ.get("CLODIA_TOOLS_TOPICS_URL")
    if explicit:
        return explicit.rstrip("/")
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/").rstrip("/")
    if mcp.endswith("/mcp"):
        mcp = mcp[: -len("/mcp")]
    return f"{mcp}/internal/topics"


def _headers() -> dict[str, str]:
    """Il token delle chiamate che l'agent-server fa al gateway PER SÉ.

    Porta `role: agent-server` (clodia-platform#539 §1): sulle rotte che
    amministrano un topic il gateway riconosceva questo chiamante dal fatto che
    il token NON aveva `execution_id` né `chat` — cioè per sottrazione, e un
    token d'agente a cui mancassero entrambi avrebbe scritto i muri di una
    stanza. Qui l'identità è affermata e firmata.
    """
    token = pki.mint_session_token(_PRINCIPAL, ttl_seconds=_TOKEN_TTL,
                                   role=pki.ROLE_AGENT_SERVER)
    return {"Authorization": f"Bearer {token}"}


def list_topics(tier: str | None = None, include_archived: bool = False) -> list[dict]:
    params = {}
    if tier:
        params["tier"] = tier
    if include_archived:
        params["include_archived"] = "true"
    try:
        r = _gw_http.get(_base(), headers=_headers(), params=params, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway topics irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway topics → HTTP {r.status_code}: {r.text[:160]}")
    return r.json().get("topics", [])


def open_topic(tier: str, name: str) -> dict | None:
    url = f"{_base()}/{tier}/{name}"
    try:
        r = _gw_http.get(url, headers=_headers(), timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway topics irraggiungibile: {e}") from e
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise TopicsClientError(f"gateway topic open → HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def create_topic(tier: str, name: str, meta: dict) -> dict:
    """Crea il topic sul gateway. Nessun hook, nessun segreto: la superficie è
    uscita con clodia-platform#223."""
    try:
        r = _gw_http.post(_base(), headers=_headers(),
                          json={"tier": tier, "name": name, "meta": meta},
                          timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway create_topic irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway create_topic → HTTP {r.status_code}: {r.text[:160]}")
    return r.json().get("meta", {})



def read_topic_logo(tier: str, name: str) -> tuple[bytes, str]:
    """I byte del logo, col tipo che il gateway ha rilevato al caricamento.

    Passa dalla rotta dedicata e non da quella generica dei file: il logo sta nel
    control plane, mentre `/file` risolve i path del data plane — su un topic con
    remote Drive lo cercherebbe su Drive, dove non è mai stato scritto.
    """
    url = f"{_base()}/{tier}/{name}/logo"
    try:
        r = _gw_http.get(url, headers=_headers(), timeout=_HTTP_TIMEOUT_ESTERNO)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway file irraggiungibile: {e}") from e
    if r.status_code >= 400:
        raise TopicsClientError(f"file non disponibile ({r.status_code})",
                                status=r.status_code)
    return r.content, r.headers.get("content-type", "application/octet-stream")


def topic_logo(tier: str, name: str, payload: dict | None) -> dict:
    """Imposta (payload con `data` base64) o toglie (payload None) il logo."""
    url = f"{_base()}/{tier}/{name}/logo"
    try:
        r = (_gw_http.delete(url, headers=_headers(), timeout=_HTTP_TIMEOUT_ESTERNO)
             if payload is None else
             _gw_http.post(url, headers=_headers(), json=payload,
                           timeout=_HTTP_TIMEOUT_ESTERNO))
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway logo irraggiungibile: {e}") from e
    if r.status_code >= 400:
        try:
            det = (r.json() or {}).get("error") or r.text[:200]
        except Exception:  # noqa: BLE001
            det = r.text[:200]
        raise TopicsClientError(det)
    return r.json()


def mcp_clients(tier: str, name: str, payload: dict | None = None) -> dict:
    """Client MCP umani di un topic. `payload` None → elenco; altrimenti azione."""
    url = f"{_base()}/{tier}/{name}/mcp-clients"
    try:
        if payload is None:
            r = _gw_http.get(url, headers=_headers(), timeout=_HTTP_TIMEOUT)
        else:
            r = _gw_http.post(url, headers=_headers(), json=payload,
                              timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway mcp-clients irraggiungibile: {e}") from e
    if r.status_code >= 400:
        # Intatto: dice QUALE condizione ha fermato la coniazione (tier troppo
        # alto, provider non dichiarato, consenso mancante) e ognuna ha un
        # rimedio diverso.
        try:
            det = (r.json() or {}).get("error") or r.text[:200]
        except Exception:  # noqa: BLE001
            det = r.text[:200]
        raise TopicsClientError(det)
    return r.json()


def archive_topic(tier: str, name: str) -> dict:
    url = f"{_base()}/{tier}/{name}/archive"
    try:
        r = _gw_http.post(url, headers=_headers(), timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway archive irraggiungibile: {e}") from e
    if r.status_code >= 400:
        raise TopicsClientError(f"gateway archive → HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def set_status(tier: str, name: str, status: str) -> dict:
    url = f"{_base()}/{tier}/{name}/status"
    try:
        r = _gw_http.post(url, headers=_headers(), json={"status": status}, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway set-status irraggiungibile: {e}") from e
    if r.status_code >= 400:
        raise TopicsClientError(f"gateway set-status → HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def set_deadline(tier: str, name: str, deadline: str | None) -> dict:
    url = f"{_base()}/{tier}/{name}/deadline"
    try:
        r = _gw_http.post(url, headers=_headers(), json={"deadline": deadline}, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway set-deadline irraggiungibile: {e}") from e
    if r.status_code >= 400:
        raise TopicsClientError(f"gateway set-deadline → HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def set_goal(tier: str, name: str, goal: dict | None, by: str = "") -> dict:
    """Fissa o toglie l'OBIETTIVO del canale (clodia-platform#457).

    `by` è il principal di cui QUESTO servizio ha verificato la proprietà del
    canale: il gateway non conosce i ruoli umani di uno scope e si fida di
    questo campo, quindi non deve mai arrivare dal corpo della richiesta HTTP.
    """
    url = f"{_base()}/{tier}/{name}/goal"
    try:
        r = _gw_http.post(url, headers=_headers(), json={"goal": goal, "by": by},
                          timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway set-goal irraggiungibile: {e}") from e
    if r.status_code >= 400:
        raise TopicsClientError(f"gateway set-goal → HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def get_agents_md(tier: str, name: str) -> tuple[str | None, str | None, bool]:
    """`(testo, versione, autorevole)` delle istruzioni di scope.

    `autorevole` distingue il control-plane dal fallback legacy in `files/`, dove
    QUALUNQUE partecipante poteva scrivere. Chi inietta questo testo in un prompt
    deve poterlo sapere: è la differenza fra una nota di canale e una direttiva.
    """
    url = f"{_base()}/{tier}/{name}/agents-md"
    try:
        r = _gw_http.get(url, headers=_headers(), timeout=_HTTP_TIMEOUT_ESTERNO)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway agents-md irraggiungibile: {e}") from e
    if r.status_code >= 400:
        raise TopicsClientError(f"gateway agents-md → HTTP {r.status_code}: {r.text[:160]}")
    d = r.json()
    return d.get("text"), d.get("version"), bool(d.get("authoritative"))


def save_agents_md(tier: str, name: str, text: str,
                   base_version: str | None) -> dict:
    """Riscrive le istruzioni di scope. 409 = qualcun altro ha scritto nel
    frattempo: il chiamante rilegge e rifonde, non sovrascrive."""
    url = f"{_base()}/{tier}/{name}/agents-md"
    try:
        r = _gw_http.post(url, headers=_headers(),
                          json={"text": text, "base_version": base_version},
                          timeout=_HTTP_TIMEOUT_ESTERNO)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway agents-md irraggiungibile: {e}") from e
    if r.status_code == 409:
        raise TopicsConflictError(r.json().get("error") or "conflitto di versione")
    if r.status_code >= 400:
        raise TopicsClientError(f"gateway agents-md → HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def list_messages(tier: str, name: str, limit: int = 200) -> list[dict]:
    url = f"{_base()}/{tier}/{name}/messages"
    try:
        r = _gw_http.get(url, headers=_headers(), params={"limit": limit}, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway messages irraggiungibile: {e}") from e
    if r.status_code == 404:
        return []
    if r.status_code != 200:
        raise TopicsClientError(f"gateway messages → HTTP {r.status_code}: {r.text[:160]}")
    return r.json().get("messages", [])


def post_message(tier: str, name: str, author: str, text: str,
                 kind: str = "human", attachments: list[str] | None = None) -> dict:
    url = f"{_base()}/{tier}/{name}/messages"
    body = {"author": author, "text": text, "kind": kind, "attachments": attachments or []}
    try:
        r = _gw_http.post(url, headers=_headers(), json=body, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway post_message irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway post_message → HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def set_participant(tier: str, name: str, agent: str, add: bool = True,
                    role: str | None = None) -> dict:
    """Invita, rimuove, o CAMBIA il ruolo di chi è già dentro.

    `role`: `contributor` (default; `reader` è abolito dal 24 ott 2026). Cambiare ruolo passa da qui e non
    da togli-e-rimetti, che nel frattempo farebbe uscire la persona dal canale e
    le manderebbe un messaggio di uscita e uno di rientro per un cambio di grado.
    """
    url = f"{_base()}/{tier}/{name}/participants"
    method = _gw_http.post if add else _gw_http.delete
    payload = {"agent": agent}
    if add and role:
        payload["role"] = role
    try:
        r = method(url, headers=_headers(), json=payload, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway participants irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway participants → HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def drive_folder_action(tier: str, name: str, action: str, **params) -> dict:
    """Cartelle Drive dichiarate per un canale (add/remove/set_credential) →
    gateway. Decision-record #40: nessun mount da navigare, solo la
    dichiarazione di perimetro per gdrive.*."""
    url = f"{_base()}/{tier}/{name}/drive-folder"
    try:
        r = _gw_http.post(url, headers=_headers(), json={"action": action, **params},
                          timeout=60)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway drive-folder irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise _http_error("drive-folder", r)
    return r.json()


def topic_data_action(tier: str, name: str, body: dict) -> dict:
    """Data imported by a topic (#590): import/remove/grant → gateway."""
    url = f"{_base()}/{tier}/{name}/data"
    try:
        r = _gw_http.post(url, headers=_headers(), json=body, timeout=60)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway data irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise _http_error("data", r)
    return r.json()


def topic_index_action(tier: str, name: str, body: dict) -> dict:
    """The topic's RAG index (#599): enable/disable → gateway."""
    url = _base().rsplit("/topics", 1)[0] + f"/topic-index/{tier}/{name}"
    try:
        r = _gw_http.post(url, headers=_headers(), json=body, timeout=120)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway topic-index irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise _http_error("topic-index", r)
    return r.json()


def data_catalog(body: dict | None = None) -> dict:
    """The instance data catalog (#590): GET without `body`, POST with it."""
    url = _base().rsplit("/topics", 1)[0] + "/data-catalog"
    try:
        if body is None:
            r = _gw_http.get(url, headers=_headers(), timeout=60)
        else:
            r = _gw_http.post(url, headers=_headers(), json=body, timeout=300)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway data-catalog irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise _http_error("data-catalog", r)
    return r.json()


def topic_tools(tier: str, name: str, body: dict | None = None) -> dict:
    """The room's toolbox (#605/#609): GET without `body`, POST with it.

    GET → `{enabled, except, enablable, gated}`; POST →
    `{action: enable|disable|except|unexcept, verb, agent?}`. The gateway
    decides what is enablable and re-checks the caller: this side only says
    WHO may ask (`topic_tools.py`)."""
    url = f"{_base()}/{tier}/{name}/tools"
    try:
        if body is None:
            r = _gw_http.get(url, headers=_headers(), timeout=60)
        else:
            r = _gw_http.post(url, headers=_headers(), json=body, timeout=60)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway tools irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise _http_error("tools", r)
    return r.json()


def dm_tools(body: dict | None = None) -> dict:
    """The verbs enabled OUTSIDE a room (#605, decision 5 of epic #604):
    GET without `body`, POST with it. An instance setting, not a topic's:
    there is no room to read it from."""
    url = _base().rsplit("/topics", 1)[0] + "/dm-tools"
    try:
        if body is None:
            r = _gw_http.get(url, headers=_headers(), timeout=60)
        else:
            r = _gw_http.post(url, headers=_headers(), json=body, timeout=60)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway dm-tools irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise _http_error("dm-tools", r)
    return r.json()


def local_folder_action(tier: str, name: str, action: str, **params) -> dict:
    """Cartella condivisa Mac↔container per un topic (add/remove) → gateway.

    A differenza di `drive_folder_action`, qui il collegamento È un
    filesystem vero (bind su una radice unica, `LOCAL_SHARED_ROOT` lato
    gateway): non uno specchio, non un perimetro dichiarato — vedi
    `TopicService.local_folder_add`."""
    url = f"{_base()}/{tier}/{name}/local-folder"
    try:
        r = _gw_http.post(url, headers=_headers(), json={"action": action, **params},
                          timeout=60)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway local-folder irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise _http_error("local-folder", r)
    return r.json()


def mailbox_link_status(tier: str, name: str) -> dict:
    """Caselle email del sistema e stato di autorizzazione in questo topic →
    gateway (clodia-platform#406).

    Le caselle vivono nella vault del gateway, non qui: questo è l'unico modo
    per conoscerne l'INDIRIZZO, che è la forma che finisce nelle liste
    `inbox:`/`outbox:` dello scope."""
    url = f"{_base()}/{tier}/{name}/mailbox-link"
    try:
        r = _gw_http.get(url, headers=_headers(), timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway mailbox-link irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise _http_error("mailbox-link", r)
    return r.json()


def mailbox_link_action(tier: str, name: str, action: str, **params) -> dict:
    """Collega/scollega una casella di sistema a questo topic → gateway.

    Nessun binding (a differenza del bottone Telegram): qui si scrive solo la
    whitelist per-scope nelle due direzioni."""
    url = f"{_base()}/{tier}/{name}/mailbox-link"
    try:
        r = _gw_http.post(url, headers=_headers(), json={"action": action, **params},
                          timeout=60)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway mailbox-link irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise _http_error("mailbox-link", r)
    return r.json()


#: Tetto di lunghezza di un segmento. Un id che il gateway possa davvero avere
#: sta in 40 caratteri (`connectors/manifest._ID`, i namespace in 32): 64
#: lascia margine e non taglia nulla di legittimo (clodia-platform#545 §3).
MAX_SEGMENT = 64

#: What a connector id or an action id may look like in a gateway URL. The
#: gateway's own ids are narrower (`^[a-z][a-z0-9-]*$`); this is the wall that
#: keeps a dot segment or a slash out of the path (review of #521: `..` was
#: normalised away by `requests`, and `connectors/../mailbox-link` reached the
#: specific route on the agent-server's trusted token). Il tetto di lunghezza
#: è parte del pattern: senza, cinquemila caratteri passavano di qui e
#: arrivavano al gateway (#545 §3).
CONNECTOR_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,%d}$" % (MAX_SEGMENT - 1))


def _segment(value: str, what: str) -> str:
    """`value` checked against `CONNECTOR_SEGMENT`, then URL-quoted anyway."""
    value = str(value or "")
    if not CONNECTOR_SEGMENT.fullmatch(value):
        raise TopicsClientError(f"{what} non valido: '{value[:64]}'",
                                status=400, detail=f"{what} non valido")
    return quote(value, safe="")


def _connector_url(tier: str, name: str, connector: str, action: str) -> str:
    return (f"{_base()}/{tier}/{name}/connectors/"
            f"{_segment(connector, 'connettore')}/{_segment(action, 'azione')}")


def _internal_base() -> str:
    """`…/internal`, the root of the gateway's server-to-server routes."""
    base = _base()
    return base[: -len("/topics")] if base.endswith("/topics") else base


def connectors_registry() -> dict:
    """`GET /internal/connectors` with the agent-server's OWN token (#520).

    The gateway serves the registry to admins and to the agent-server
    (`role: agent-server`), never to a plain user session: the route that calls
    this has already decided who may see what, and reduces the payload to what
    a topic page draws before it leaves this process.
    """
    url = f"{_internal_base()}/connectors"
    try:
        r = _gw_http.get(url, headers=_headers(), timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway connectors irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise _http_error("connectors", r)
    return r.json()


def connector_action_status(tier: str, name: str, connector: str,
                            action: str) -> dict:
    """Stato di UNA azione di connettore su questo topic → gateway (#520).

    La forma generica di `mailbox_link_status`: quali
    campi l'azione chiede, quali valori l'istanza offre, quali voci ha già
    scritto in questa stanza, il tier del topic e — se l'azione aggancia un
    canale — lo stato del cap SEAL. Porta anche `admin_only`, che è ciò che
    questo lato deve sapere PRIMA di inoltrare una POST: il ruolo dell'umano lo
    conosce solo l'agent-server, perché al gateway la chiamata arriva con la
    sessione che l'agent-server conia per sé.
    """
    url = _connector_url(tier, name, connector, action)
    try:
        r = _gw_http.get(url, headers=_headers(), timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway connectors irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise _http_error(f"connectors/{connector}/{action}", r)
    return r.json()


def connector_action_apply(tier: str, name: str, connector: str, action: str,
                           body: dict) -> dict:
    """Esegue l'azione dichiarata dal manifest → gateway (#520).

    Il corpo si inoltra COM'È: i campi li dichiara il manifest del connettore,
    e un client che ne conoscesse i nomi per filtrarli sarebbe il per-connettore
    che questa rotta esiste per togliere. Ciò che il corpo può scrivere non lo
    decide comunque il chiamante — `connectors/actions.check_uris` nel gateway
    accetta solo gli URI degli schemi che quel connettore possiede.
    """
    url = _connector_url(tier, name, connector, action)
    try:
        r = _gw_http.post(url, headers=_headers(), json=dict(body or {}),
                          timeout=60)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway connectors irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise _http_error(f"connectors/{connector}/{action}", r)
    return r.json()


def set_topic_tier(tier: str, name: str, new_tier: str, by: str, reason: str) -> dict:
    """Riclassifica il topic a un nuovo livello SEAL → gateway
    (clodia-platform#426). Chi può farlo lo verifica il chiamante."""
    url = f"{_base()}/{tier}/{name}/tier"
    try:
        r = _gw_http.post(url, headers=_headers(),
                          json={"tier": new_tier, "by": by, "reason": reason}, timeout=120)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway riclassificazione irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise _http_error("tier", r)
    return r.json()


def list_files(tier: str, name: str, subpath: str = "") -> list[dict]:
    url = f"{_base()}/{tier}/{name}/files"
    try:
        r = _gw_http.get(url, headers=_headers(), params={"path": subpath},
                         timeout=_HTTP_TIMEOUT_ESTERNO)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway files irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway files → HTTP {r.status_code}: {r.text[:160]}")
    return r.json().get("files", [])


def clear_taint(tier: str, name: str, by: str = "") -> dict:
    """Azzera il primo bit del canale: l'owner approva lo stato corrente.

    Chi verifica che il richiedente sia l'owner è QUI (l'agent-server conosce i
    ruoli dello scope); il gateway esegue. Le sorgenti non si perdono: `clear`
    le archivia, così l'audit può ancora dire cosa era entrato prima.
    """
    url = f"{_base()}/{tier}/{name}/taint/clear"
    try:
        r = _gw_http.post(url, headers=_headers(), json={"by": by},
                          timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway taint irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway taint → HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def read_file(tier: str, name: str, path: str) -> bytes:
    """Byte grezzi di un file del topic (binario incluso). Usato dal proxy
    file-per-l'agente per scaricare un deliverable in scratch senza farlo
    transitare in base64 dal modello."""
    url = f"{_base()}/{tier}/{name}/file"
    try:
        r = _gw_http.get(url, headers=_headers(), params={"path": path}, timeout=_HTTP_TIMEOUT_ESTERNO)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway read_file irraggiungibile: {e}") from e
    if r.status_code == 404:
        raise TopicsClientError(f"file non trovato: {path}")
    if r.status_code != 200:
        raise TopicsClientError(f"gateway read_file → HTTP {r.status_code}: {r.text[:160]}")
    return r.content


def put_file(tier: str, name: str, filename: str, content_b64: str,
             provenance: str = "untrusted") -> dict:
    """Carica un file nel topic. `provenance` = `trusted` | `untrusted`.

    Default `untrusted` (clodia-platform#104 §3): se il chiamante non dichiara la
    provenienza non si assume il bene. Un file untrusted contamina il canale — la
    lettura resta libera, è una classificazione e non un blocco.
    """
    url = f"{_base()}/{tier}/{name}/files"
    try:
        r = _gw_http.post(url, headers=_headers(),
                          json={"filename": filename, "content_b64": content_b64,
                                "provenance": provenance},
                          timeout=_HTTP_TIMEOUT_ESTERNO)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway put_file irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway put_file → HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def export_bundle(topics: list[str] | None = None) -> bytes:
    """Scarica dal gateway il tar.gz dei topic (snapshot). `topics` = lista di
    'tier/name' da includere; None → tutti."""
    url = f"{_base()}/export"
    params = {"topics": ",".join(topics)} if topics else None
    try:
        r = _gw_http.get(url, headers=_headers(), params=params, timeout=300)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway export irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway export → HTTP {r.status_code}: {r.text[:160]}")
    return r.content


def import_bundle(data: bytes) -> dict:
    """Invia al gateway il tar.gz da importare (merge non-distruttivo)."""
    url = f"{_base()}/import"
    headers = {**_headers(), "Content-Type": "application/gzip"}
    try:
        r = _gw_http.post(url, headers=headers, data=data, timeout=300)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway import irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway import → HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def get_file(tier: str, name: str, path: str) -> bytes | None:
    """Byte di un file dentro il topic (es. files/foo.md), via gateway. None se 404."""
    url = f"{_base()}/{tier}/{name}/file"
    try:
        r = _gw_http.get(url, headers=_headers(), params={"path": path}, timeout=_HTTP_TIMEOUT_ESTERNO)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway topic file irraggiungibile: {e}") from e
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise TopicsClientError(f"gateway topic file → HTTP {r.status_code}: {r.text[:160]}")
    return r.content


def _async_of(sync_name: str):
    """Wrapper async TRASPARENTE della funzione sincrona `sync_name`.

    Inoltra la chiamata a un thread **così com'è**: gli argomenti arrivano alla
    funzione sincrona nella stessa forma in cui il chiamante li ha scritti. Un
    wrapper che ricopia la firma ed espande i default cambia quella forma —
    `post_message(t, n, a, testo, kind=...)` diventava
    `post_message(t, n, a, testo, kind, attachments)` — e chi sostituisce la
    funzione (un fake in un test, un adattatore) la vede diversa da come il
    chiamante l'ha invocata.

    La funzione bersaglio si risolve al momento della chiamata, per nome: così
    `patch.object(topics_client, "post_message", ...)` continua a mordere anche
    passando dal wrapper.
    """
    async def wrapper(*args, **kwargs):
        return await _to_thread(globals()[sync_name], *args, **kwargs)

    wrapper.__name__ = wrapper.__qualname__ = f"async_{sync_name}"
    wrapper.__doc__ = f"Versione async di `{sync_name}`: stessa firma, attesa in un thread."
    return wrapper


# I wrapper async dei client: da un handler `async def` si chiama SEMPRE questo
# lato, mai la funzione sincrona — il test `test_no_sync_http_in_async_handlers`
# lo verifica sull'AST di tutto `server/`.
async_list_topics = _async_of("list_topics")
async_open_topic = _async_of("open_topic")
async_create_topic = _async_of("create_topic")
async_archive_topic = _async_of("archive_topic")
async_get_agents_md = _async_of("get_agents_md")
async_save_agents_md = _async_of("save_agents_md")
async_list_messages = _async_of("list_messages")
async_post_message = _async_of("post_message")
async_drive_folder_action = _async_of("drive_folder_action")
async_local_folder_action = _async_of("local_folder_action")
async_mailbox_link_status = _async_of("mailbox_link_status")
async_mailbox_link_action = _async_of("mailbox_link_action")
async_connector_action_status = _async_of("connector_action_status")
async_connector_action_apply = _async_of("connector_action_apply")
async_connectors_registry = _async_of("connectors_registry")
async_set_topic_tier = _async_of("set_topic_tier")
async_list_files = _async_of("list_files")
async_get_file = _async_of("get_file")
async_read_file = _async_of("read_file")
async_put_file = _async_of("put_file")
async_set_participant = _async_of("set_participant")
async_mcp_clients = _async_of("mcp_clients")
async_clear_taint = _async_of("clear_taint")
async_topic_logo = _async_of("topic_logo")
async_read_topic_logo = _async_of("read_topic_logo")
async_set_status = _async_of("set_status")
async_set_deadline = _async_of("set_deadline")
async_set_goal = _async_of("set_goal")
async_export_bundle = _async_of("export_bundle")
async_import_bundle = _async_of("import_bundle")
