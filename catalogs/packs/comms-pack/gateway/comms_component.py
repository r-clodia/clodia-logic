"""comms-pack's ONE gateway component: mail and Telegram.

The gateway allows one `target: gateway` component per pack — its pin carries
the pack's manifest (clodia-tools `components/store.py`). comms-pack 2.1.0
declared two (`mail`, `telegram`) and the second was refused at load. This
entry registers both halves from the same pinned tree, each only where the
instance hands its connector to the pack (`host.serves`): `mail: pack` →
the eight `email.*` verbs and the mailbox services (`mail_component.py`);
`telegram: pack` → the Telegram verbs and the relay's services
(`telegram_component.py`). A connector left `native` is the gateway's.
"""
from __future__ import annotations

from typing import Any


def register(host: Any) -> None:
    if host.serves("email"):
        from . import mail_component
        mail_component.register(host)
    if host.serves("telegram"):
        from . import telegram_component
        telegram_component.register(host)
