"""Adapter di trasporto del relay: cosa deve sapere fare un canale per entrare.

clodia-platform#521 (epic #527). Fino al 14 ott 2026 `channel_relay` *era*
Telegram: il modello del relay — buffer di contesto, attivazione su menzione o
reply, mittente vagliato contro gli ingress dello scope, gruppo in whitelist,
testo del rifiuto — stava scritto una volta sola ma dentro un trasporto solo.
Il secondo canale avrebbe copiato quel file, e due copie di una regola di
autorizzazione divergono alla prima modifica, in silenzio.

Qui c'è il CONTRATTO; il modello resta in `channel_relay`, uno solo per tutti.
Un adapter fa quattro cose, quelle della issue:

1. **inbound** — `poll(timeout)`: i messaggi nuovi di tutte le chat del
   trasporto;
2. **outbound** — `send(chat_id, text)`, e `download(file_ref)` se porta
   allegati;
3. **identità** — `sender_uri(msg)` e `chat_uri(chat_id)`: chi ha scritto e
   dove, nella forma di un URI di ingress. Il relay non decide chi è
   autorizzato: trasforma l'identità in un URI e la domanda la fa al gateway
   (clodia-platform#364/#365). Un mittente che il trasporto non sa nominare in
   quella forma vale `None`, cioè «non vagliabile», cioè rifiutato;
4. **attivazione** — `addresses_bot(msg, participants)`: il messaggio interpella
   il bot? Il default qui sotto (menzione di un agente, oppure reply a un
   messaggio del bot) è la regola della piattaforma e vale per ogni trasporto:
   un adapter la riscrive solo se il suo trasporto ha un'altra idea di menzione.

**Contratto del messaggio inbound** — un dizionario con le chiavi:
`message_id` (univoco nella chat), `chat_id`, `text`, `from_id`,
`from_username` (l'identità AUTENTICATA del trasporto, mai ciò che il testo
dichiara), `from` (come mostrarla), `chat_title`, `reply_to_bot` (bool),
`file` (`{file_id, file_name}`, facoltativo).

Il registro è una tabella id→modulo invece di un import diretto: così il
processo non importa il client di un trasporto che l'istanza non usa, e non ci
sono cicli (l'adapter importa il suo client, il nucleo importa il registro).
Quando #519 darà gli endpoint generici dei connettori, questa tabella diventerà
la proiezione del registry dei pack — il punto in cui cambiarla è uno.
"""
from __future__ import annotations

import importlib
import logging

LOG = logging.getLogger("agent-server.relay_adapters")

#: id del connettore (chiave dei binding) → modulo che espone `ADAPTER`.
_BUILTIN = {"telegram": "server.api.telegram_relay_adapter"}

_CACHE: dict = {}


def _addresses_bot(text: str, participants: list) -> bool:
    """True se il messaggio INTERPELLA il bot o un agente del topic (menzione)."""
    t = (text or "").lower()
    if "@clodia" in t:
        return True
    for p in (participants or []):
        pl = str(p).lower()
        if pl and f"@{pl}" in t:
            return True
    return False


class RelayAdapter:
    """Base con i comportamenti neutri. Un adapter implementa il trasporto."""

    #: id del connettore come appare nei binding (`channel-bindings.json`).
    connector = ""
    #: come il trasporto si chiama nel testo che arriva nel canale («telegram»).
    transport_label = ""
    #: schema degli URI di ingress di questo trasporto («tg»), per i messaggi
    #: d'errore e per chi legge: gli URI veri li costruiscono i metodi sotto.
    uri_scheme = ""

    # ── inbound / outbound ───────────────────────────────────────────────────
    async def poll(self, timeout: int) -> list:
        raise NotImplementedError

    async def send(self, chat_id, text: str, thread_id=None) -> None:
        """`thread_id`: dove rispondere dentro la chat, se il trasporto ha i
        thread (topic di un forum Telegram, clodia-platform#548); None = come
        prima."""
        raise NotImplementedError

    async def download(self, file_ref: dict) -> str | None:
        """Il contenuto dell'allegato in base64, o None se il trasporto non
        porta allegati (il relay lo marca come non scaricato, non esplode)."""
        return None

    # ── identità ─────────────────────────────────────────────────────────────
    def sender_uri(self, msg: dict) -> str | None:
        raise NotImplementedError

    def chat_uri(self, chat_id) -> str | None:
        raise NotImplementedError

    # ── attivazione e presentazione ──────────────────────────────────────────
    def addresses_bot(self, msg: dict, participants: list) -> bool:
        return (_addresses_bot((msg or {}).get("text") or "", participants)
                or bool((msg or {}).get("reply_to_bot")))

    def neutralise_mentions(self, text: str) -> str:
        """Le menzioni del TRASPORTO non devono convocare agenti nel canale.
        Chi non ha una sintassi di menzione propria non ha niente da fare."""
        return text or ""

    def chat_label(self, msg: dict, chat_id) -> str:
        return (msg or {}).get("chat_title") or str(chat_id)

    def sender_label(self, msg: dict) -> str:
        m = msg or {}
        return str(m.get("from") or m.get("from_username")
                   or m.get("from_id") or "?").lstrip("@")


def get(connector: str):
    """L'adapter di un connettore, o None se l'istanza non ne ha uno.

    Un binding che nomina un trasporto sconosciuto non è un errore fatale: è un
    pack disinstallato o non ancora installato, e il relay deve continuare a
    servire gli altri canali invece di morire sul primo nome che non conosce.
    """
    cid = str(connector or "")
    if cid in _CACHE:
        return _CACHE[cid]
    modulo = _BUILTIN.get(cid)
    adapter = None
    if modulo:
        try:
            adapter = getattr(importlib.import_module(modulo), "ADAPTER", None)
        except Exception as e:  # noqa: BLE001
            LOG.warning("adapter '%s' non caricabile (%s)", cid, e)
    if adapter is None:
        LOG.warning("binding sul connettore '%s': nessun adapter di relay, chat ignorate", cid)
    _CACHE[cid] = adapter
    return adapter


def register(adapter) -> None:
    """Registra un adapter già costruito (lo usano i test e, domani, i pack)."""
    _CACHE[adapter.connector] = adapter


def forget(connector: str) -> None:
    _CACHE.pop(str(connector or ""), None)
