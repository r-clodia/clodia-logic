"""Nessuna chat legata → nessun `getUpdates`.

Due istanze che pollano lo stesso bot Telegram si terminano a vicenda con
`409 Conflict: terminated by other getUpdates`, e il gateway risponde 502 al
relay. Misurato il 17 ago 2026 fra terra e venere: **864 errori in due ore** su
una, 247 in trenta minuti sull'altra, con **zero binding da entrambe le parti**.

Due danni, e il secondo è quello che è costato di più: nessuna delle due riceveva
Telegram in modo affidabile, e quei 864 errori hanno nascosto per un giorno la
causa vera di un guasto diverso — chi cercava un colpevole nei log trovava questi
e ci costruiva sopra una spiegazione plausibile e falsa.

Il rimedio non richiede di scegliere quale istanza tenga il bot: se non c'è
niente da instradare, non si chiama Telegram.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channel_relay


class PollOnlyWithBindingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_bindings_means_no_call_to_telegram(self) -> None:
        """`run_poll_cycle` da sola POLLA comunque: è il loop a decidere. Qui si
        fissa che il ciclo, se interrogato, non instradi nulla senza binding —
        l'altra metà (non chiamare affatto) è nel loop di `main`."""
        with patch.object(channel_relay.telegram_client, "poll",
                          return_value=[{"chat_id": "999", "text": "ciao"}]), \
             patch.object(channel_relay.tb, "load", return_value={}), \
             patch.object(channel_relay, "_relay_chat") as relay:
            serviti = await channel_relay.run_poll_cycle(1)
        self.assertEqual(0, serviti)
        relay.assert_not_called()

    async def test_a_bound_chat_is_served(self) -> None:
        with patch.object(channel_relay.telegram_client, "poll",
                          return_value=[{"chat_id": "42", "text": "ciao"}]), \
             patch.object(channel_relay.tb, "load",
                          return_value={"42": {"tier": "SEAL-1", "name": "ops"}}), \
             patch.object(channel_relay, "_relay_chat") as relay:
            serviti = await channel_relay.run_poll_cycle(1)
        self.assertEqual(1, serviti)
        relay.assert_called_once()

    async def test_an_unbound_chat_among_bound_ones_is_ignored(self) -> None:
        """Il caso che tiene onesto il conteggio: due chat, una sola legata."""
        with patch.object(channel_relay.telegram_client, "poll",
                          return_value=[{"chat_id": "42", "text": "a"},
                                        {"chat_id": "999", "text": "b"}]), \
             patch.object(channel_relay.tb, "load",
                          return_value={"42": {"tier": "SEAL-1", "name": "ops"}}), \
             patch.object(channel_relay, "_relay_chat") as relay:
            serviti = await channel_relay.run_poll_cycle(1)
        self.assertEqual(1, serviti)
        self.assertEqual(1, relay.call_count)


class TheLoopChecksBeforePollingTests(unittest.TestCase):
    """Il loop di `main` deve interrogare i binding PRIMA di chiamare Telegram.

    Senza questo controllo il conflitto resta: `run_poll_cycle` blocca su
    `getUpdates` per venticinque secondi e solo dopo scopre che non c'era niente
    da fare — e nel frattempo ha terminato il long-poll dell'altra istanza.
    """

    def test_the_loop_consults_the_bindings_first(self) -> None:
        import inspect
        from .. import main
        src = inspect.getsource(main.startup) if hasattr(main, "startup") else ""
        if not src:
            # il loop vive dentro l'handler di startup: si cerca nel modulo
            src = inspect.getsource(main)
        i_check = src.find("if not _tb.load()")
        i_poll = src.find("run_poll_cycle(timeout)")
        self.assertGreater(i_check, -1, "il loop non consulta i binding")
        self.assertGreater(i_poll, i_check,
                           "il poll avviene PRIMA del controllo: il conflitto resta")


if __name__ == "__main__":
    unittest.main()
