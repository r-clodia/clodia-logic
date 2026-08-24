"""Client verso gli endpoint interni dei topic del gateway (Topic System v2).

Stesso pattern di provider_store/imagegen_client: il runner di clodia-logic fa da
proxy per la webui, chiamando il gateway (`/internal/topics`) con un token ckt1
firmato per il principal `clodia`. I topic v2 vivono dietro il gateway; qui li
leggiamo solo per servirli alla pagina Topics.
"""
from __future__ import annotations

import os
import asyncio

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
    return {"Authorization": f"Bearer {pki.mint_session_token(_PRINCIPAL, ttl_seconds=_TOKEN_TTL)}"}


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


def create_topic(tier: str, name: str, meta: dict,
                 hook_enabled: bool = False) -> dict:
    """Un topic nuovo non nasce con un hook: la porta pubblica e il suo segreto
    si creano solo se qualcuno li chiede (clodia-tools#211)."""
    try:
        r = _gw_http.post(_base(), headers=_headers(),
                          json={"tier": tier, "name": name, "meta": meta,
                                "hook_enabled": hook_enabled,
                                # Evita il ciclo logic → gateway → logic: questo
                                # client assicura l'hook localmente dopo il POST.
                                "ensure_hook": False},
                          timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway create_topic irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway create_topic → HTTP {r.status_code}: {r.text[:160]}")
    created = r.json().get("meta", {})
    if hook_enabled:
        from ..hooks import db as hooks_db
        try:
            hooks_db.ensure(
                created.get("tier", tier), name, name,
                created_by=meta.get("owner") or _PRINCIPAL)
        except hooks_db.HookConflictError as e:
            raise TopicsClientError(str(e)) from e
    return created



def telegram_binding(tier: str, name: str, payload: dict) -> dict:
    url = f"{_base()}/{tier}/{name}/telegram"
    try:
        r = _gw_http.post(url, headers=_headers(), json=payload, timeout=_HTTP_TIMEOUT_ESTERNO)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway telegram irraggiungibile: {e}") from e
    if r.status_code >= 400:
        # Il messaggio del gateway arriva INTATTO: dice quale delle cinque
        # verifiche ha fermato il collegamento (cap, url pubblico, bot fuori dal
        # gruppo, mappa vuota, nome sconosciuto), e ognuna ha un rimedio diverso.
        try:
            det = (r.json() or {}).get("error") or r.text[:200]
        except Exception:  # noqa: BLE001
            det = r.text[:200]
        raise TopicsClientError(det)
    return r.json()


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


def set_portable(tier: str, name: str, portable: bool) -> dict:
    url = f"{_base()}/{tier}/{name}/portable"
    try:
        r = _gw_http.post(url, headers=_headers(), json={"portable": bool(portable)},
                          timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway portable irraggiungibile: {e}") from e
    if r.status_code >= 400:
        raise TopicsClientError(f"gateway portable → HTTP {r.status_code}: {r.text[:160]}")
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

    `role`: `contributor` (default) o `reader`. Cambiare ruolo passa da qui e non
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


def remote_action(tier: str, name: str, action: str, **params) -> dict:
    """Verbi Remote del topic (status/enable/disable/add/commit/push/pull) → gateway."""
    url = f"{_base()}/{tier}/{name}/remote"
    try:
        r = _gw_http.post(url, headers=_headers(), json={"action": action, **params},
                          timeout=60)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway remote irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise _http_error("remote", r)
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
async_remote_action = _async_of("remote_action")
async_list_files = _async_of("list_files")
async_get_file = _async_of("get_file")
async_read_file = _async_of("read_file")
async_put_file = _async_of("put_file")
async_set_participant = _async_of("set_participant")
async_mcp_clients = _async_of("mcp_clients")
async_clear_taint = _async_of("clear_taint")
async_telegram_binding = _async_of("telegram_binding")
async_topic_logo = _async_of("topic_logo")
async_read_topic_logo = _async_of("read_topic_logo")
async_set_portable = _async_of("set_portable")
async_set_status = _async_of("set_status")
async_set_deadline = _async_of("set_deadline")
async_export_bundle = _async_of("export_bundle")
async_import_bundle = _async_of("import_bundle")
