"""A relay adapter for the relay's tests (clodia-platform#683).

Telegram was the relay's first transport and its tests were written through
its adapter. Telegram is gone; the relay model (`channel_relay`) is not, and a
pack relay (WhatsApp) uses it. This adapter keeps the shape the tests were
written against — `xc:@handle` for a person, `xc:<chat_id>` for a group, a
transport with its own `@handle` mentions, 1:1 chats addressed to the bot —
over a fake `chat_client` the tests patch. Not a test module: it is imported
by them (`test_*` files only are discovered).
"""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

from . import relay_adapters
from .relay_adapters import RelayAdapter


async def _never_async(*a, **k):  # pragma: no cover — tests patch these
    raise AssertionError("chat_client not patched")


def _never(*a, **k):  # pragma: no cover
    raise AssertionError("chat_client not patched")


#: The transport's client: patched by the tests (`poll`, `send_async`,
#: `download_async`).
chat_client = SimpleNamespace(poll=_never, send_async=_never_async,
                              download_async=_never_async)


#: Un `@handle` di Telegram nel testo riportato. Nel canale lo stesso sigillo è
#: una CONVOCAZIONE: `@clodia_topics_bot` diventava la menzione di un agente che
#: non esiste, il turno restava senza destinatario e il router avvisava che
#: «non partecipa» (clodia-platform#402). Il confine sinistro è quello del parser
#: delle menzioni: un indirizzo email non è un handle.
_TG_HANDLE_RE = re.compile(r"(?<![\w@`.])@([A-Za-z][A-Za-z0-9_]{2,31})\b")


class ChatRelayAdapter(RelayAdapter):
    connector = "chat"
    transport_label = "chat"
    uri_scheme = "xc"
    post_kind = "chat"

    async def poll(self, timeout: int) -> list:
        """LONG-POLL del gateway: blocca (in un thread) fino a un messaggio o al
        timeout, e ritorna i messaggi nuovi di TUTTE le chat."""
        return await asyncio.to_thread(chat_client.poll, timeout)

    async def send(self, chat_id, text: str, thread_id=None) -> None:
        await chat_client.send_async(str(chat_id), text, thread_id)

    async def download(self, file_ref: dict) -> str | None:
        dl = await chat_client.download_async(file_ref["file_id"])
        return dl["content_b64"]

    def sender_uri(self, msg: dict) -> str | None:
        """`xc:@handle`. Un mittente senza handle non è vagliabile per
        costruzione — `xc:` in ingresso registra `@handle`, non un uid — quindi
        qui vale None, e il relay lo rifiuta senza nemmeno chiedere."""
        handle = str((msg or {}).get("from_username") or "").strip().lstrip("@")
        return f"xc:@{handle}" if handle else None

    def chat_uri(self, chat_id) -> str | None:
        """`xc:<chat_id>`: il GRUPPO come fonte vagliata (clodia-platform#402)."""
        cid = str(chat_id or "").strip()
        return f"xc:{cid}" if cid else None

    def addresses_bot(self, msg: dict, participants: list) -> bool:
        """The platform rule (a mention or a reply to the bot), plus: a 1:1
        chat with the bot (`chat_type == "private"`) is always addressed to it
        — there is no one else it could be for (owner, 30 Oct 2026,
        clodia-platform#642). Groups keep needing the mention. Who may write
        is decided later, by the sender vetting, as for every message."""
        m = msg or {}
        return super().addresses_bot(m, participants) or m.get("chat_type") == "private"

    def neutralise_mentions(self, text: str) -> str:
        """Gli `@handle` di Telegram diventano codice inline: si leggono, non
        convocano nessuno nel canale (il parser delle menzioni salta l'inline
        code)."""
        return _TG_HANDLE_RE.sub(lambda m: f"`@{m.group(1)}`", text or "")

    # ── rendering del contesto ───────────────────────────────────────────────
    # NOTA: dalla #402 il buffer viene svuotato a ogni ciclo e nel canale entra
    # solo la richiesta, quindi oggi nessuno chiama `line`/`context_block`. Il
    # rendering resta qui, col trasporto di cui porta la sintassi, invece che
    # nel nucleo neutro: se il contesto tornerà (è una decisione di prodotto,
    # non un dettaglio di codice) sarà di nuovo nel posto giusto.
    def line(self, m: dict, chat_id) -> str:
        """Una riga compatta per messaggio: `[xc://<gruppo>/<user>] -> <verbatim>`.
        `<gruppo>` = NOME/titolo della chat (leggibile), fallback al chat_id.
        `<user>` = username Telegram (identità autenticata) o uid se assente.
        Se il messaggio ha un allegato, aggiunge il riferimento al file salvato."""
        group = m.get("chat_title") or chat_id
        user = m.get("from_username") or (str(m.get("from_id"))
                                          if m.get("from_id") is not None else "?")
        text = self.neutralise_mentions((m.get("text") or "").strip())
        f = m.get("file")
        if f:
            saved = m.get("saved_file")
            tag = (f"📎 {f.get('file_name')} → salvato in `{saved}` (leggilo con "
                   f"topic.read_document/read_file)" if saved
                   else f"📎 {f.get('file_name')} (download non riuscito)")
            text = f"{text} {tag}".strip() if text else tag
        return f"[{self.uri_scheme}://{group}/{user}] -> {text}"

    def context_block(self, buffer: list, chat_id) -> str:
        """Blocco compatto: una riga per messaggio del buffer."""
        return "\n".join(self.line(m, chat_id) for m in buffer
                         if (m.get("text") or "").strip() or m.get("file"))


ADAPTER = ChatRelayAdapter()

#: Registered in-process on import: the relay finds it for the `chat`
#: connector the way it found the built-in Telegram adapter.
relay_adapters.register(ADAPTER)
