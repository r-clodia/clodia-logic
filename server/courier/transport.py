"""The courier's transport interface (clodia-platform#737).

The courier receives and transmits; a transport is the one place that knows
HOW for one medium. Email is the only transport today
(`api/mail_courier.EmailTransport`); Telegram and WhatsApp will implement the
same interface (WhatsApp in its private pack), and the courier, the outbox and
the send gate (#736) will not change for them.

What a transport owns, and what it does not:

- **reception** — `receive(only)` is one pass over the transport's sources
  for the rooms it serves: which messages are new, how they are fetched, how
  their contents land in the topic (files, provenance, the post). Priming and
  de-duplication are the transport's too, because what identifies "a message
  already seen" is medium-specific (a mail key, a Telegram update id);
- **transmission** — `send(message)` hands ONE authorised message to the
  medium. Queueing, the gate and retries are the courier's (#736), not the
  transport's: a transport is called only with a message that may leave;
- **not** the room's turn: the courier fires the room's trigger once per
  pass with what every transport delivered there (#628).

Addresses carry their scheme — `mailto:a@b.it`, later `tg:<chat>`,
`wa:<jid>` — and the scheme picks the transport.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


class TransportError(Exception):
    """A transport could not do what was asked."""


class UnsupportedScheme(TransportError):
    """No transport handles this address's scheme."""


class TransmissionUnsupported(TransportError):
    """This transport does not transmit (yet)."""


@dataclass
class ReceiveResult:
    """One reception pass of one transport.

    `by_room`: what was delivered, per `(tier, name)`, as the items the room's
    trigger lists (#628) — for mail `{from, subject, eml}`. `counters`: what
    the process view shows for the pass (#738)."""
    delivered: int = 0
    by_room: dict = field(default_factory=dict)
    counters: dict = field(default_factory=dict)


@dataclass
class Outbound:
    """One message to transmit. `to` and `sender` are addresses with their
    scheme; `room` is `(tier, name)` of the room that asked."""
    to: str
    body: str
    subject: str = ""
    sender: str = ""
    attachments: list = field(default_factory=list)
    room: Optional[tuple] = None


@dataclass
class SendResult:
    ok: bool
    id: str = ""
    error: str = ""


@runtime_checkable
class Transport(Protocol):
    #: The address scheme this transport serves (`mailto`, `tg`, `wa`).
    scheme: str
    #: Human name, for the process view and the logs.
    label: str

    async def receive(self, only: Optional[tuple] = None) -> ReceiveResult:
        """One pass over the transport's sources; `only` = one `(tier, name)`
        when an agent asked the courier to check that room now."""
        ...

    async def send(self, message: Outbound) -> SendResult:
        """Hand one authorised message to the medium. May raise
        `TransmissionUnsupported`."""
        ...


def split_address(address: str) -> tuple[str, str]:
    """`mailto:a@b.it` → `("mailto", "a@b.it")`. A bare address with `@` and
    no scheme is mail: that is what agents and people write."""
    a = str(address or "").strip()
    if not a:
        raise UnsupportedScheme("indirizzo vuoto")
    head, sep, rest = a.partition(":")
    if sep and head and head.isalpha() and rest:
        return head.lower(), rest.strip()
    if "@" in a:
        return "mailto", a
    raise UnsupportedScheme(f"indirizzo senza schema: {a!r}")
