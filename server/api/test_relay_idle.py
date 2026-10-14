"""Nessuna chat legata → nessun poll del trasporto.

Due istanze che pollano lo stesso bot Telegram si terminano a vicenda con
`409 Conflict: terminated by other getUpdates`, e il gateway risponde 502 al
relay. Misurato il 17 ago 2026 fra terra e venere: **864 errori in due ore** su
una, 247 in trenta minuti sull'altra, con **zero binding da entrambe le parti**.

Due danni, e il secondo è quello che è costato di più: nessuna delle due riceveva
Telegram in modo affidabile, e quei 864 errori hanno nascosto per un giorno la
causa vera di un guasto diverso — chi cercava un colpevole nei log trovava questi
e ci costruiva sopra una spiegazione plausibile e falsa.

Il rimedio non richiede di scegliere quale istanza tenga il bot: se non c'è
niente da instradare, non si chiama il trasporto. Dalla #521 vale per trasporto:
un connettore senza binding non viene interrogato nemmeno se un altro ne ha.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channel_relay, telegram_relay_adapter


class PollOnlyWithBindingsTests(unittest.IsolatedAsyncioTestCase):
    def _poll(self, messaggi):
        async def _p(timeout):
            self.pollato = True
            return messaggi
        self.pollato = False
        return patch.object(telegram_relay_adapter.ADAPTER, "poll", _p)

    async def test_no_bindings_means_no_call_to_telegram(self) -> None:
        """`run_poll_cycle` senza binding non interroga NESSUN trasporto: non
        c'è niente da instradare, e il poll è ciò che fa il danno."""
        with self._poll([{"chat_id": "999", "text": "ciao"}]), \
             patch.object(channel_relay.cb, "load", return_value={}), \
             patch.object(channel_relay, "_relay_chat") as relay:
            serviti = await channel_relay.run_poll_cycle(1)
        self.assertEqual(0, serviti)
        self.assertFalse(self.pollato)
        relay.assert_not_called()

    async def test_a_transport_without_bindings_is_not_polled(self) -> None:
        """L'altra faccia, nuova con la #521: i binding di un trasporto non
        autorizzano a interrogarne un altro."""
        with self._poll([{"chat_id": "42", "text": "ciao"}]), \
             patch.object(channel_relay.cb, "load",
                          return_value={"telegram": {},
                                        "signal": {"42": {"tier": "SEAL-1", "topic": "ops"}}}), \
             patch.object(channel_relay, "_relay_chat") as relay:
            serviti = await channel_relay.run_poll_cycle(1)
        self.assertEqual(0, serviti)     # nessun adapter per 'signal'
        self.assertFalse(self.pollato)   # e telegram non ha binding
        relay.assert_not_called()

    async def test_a_bound_chat_is_served(self) -> None:
        with self._poll([{"chat_id": "42", "text": "ciao"}]), \
             patch.object(channel_relay.cb, "load",
                          return_value={"telegram": {"42": {"tier": "SEAL-1", "name": "ops"}}}), \
             patch.object(channel_relay, "_relay_chat") as relay:
            serviti = await channel_relay.run_poll_cycle(1)
        self.assertEqual(1, serviti)
        relay.assert_called_once()

    async def test_an_unbound_chat_among_bound_ones_is_ignored(self) -> None:
        """Il caso che tiene onesto il conteggio: due chat, una sola legata."""
        with self._poll([{"chat_id": "42", "text": "a"}, {"chat_id": "999", "text": "b"}]), \
             patch.object(channel_relay.cb, "load",
                          return_value={"telegram": {"42": {"tier": "SEAL-1", "name": "ops"}}}), \
             patch.object(channel_relay, "_relay_chat") as relay:
            serviti = await channel_relay.run_poll_cycle(1)
        self.assertEqual(1, serviti)
        self.assertEqual(1, relay.call_count)


class BindingsAreReadAfterThePollTests(unittest.IsolatedAsyncioTestCase):
    """Review blocker on #514: the long-poll can last up to 25 s, and during it
    the gateway can unbind, move or bind a chat. Routing must use the bindings
    read AFTER the poll returns (as before #521), not the ones read before."""

    def _poll_that_changes_bindings(self, messages, before, after):
        state = {"bindings": before}

        async def _p(timeout):
            state["bindings"] = after       # the gateway writes during the poll
            return messages

        load = lambda: state["bindings"]   # noqa: E731
        return (patch.object(telegram_relay_adapter.ADAPTER, "poll", _p),
                patch.object(channel_relay.cb, "load", side_effect=load))

    async def test_a_chat_unbound_during_the_poll_is_not_served(self) -> None:
        p, l = self._poll_that_changes_bindings(
            [{"chat_id": "42", "text": "ciao"}, {"chat_id": "7", "text": "x"}],
            before={"telegram": {"42": {"tier": "SEAL-1", "topic": "ops"},
                                 "7": {"tier": "SEAL-1", "topic": "dev"}}},
            after={"telegram": {"7": {"tier": "SEAL-1", "topic": "dev"}}})
        with p, l, patch.object(channel_relay, "_relay_chat") as relay:
            served = await channel_relay.run_poll_cycle(1)
        self.assertEqual(1, served)
        self.assertEqual(["7"], [c.args[0] for c in relay.call_args_list])

    async def test_a_chat_moved_during_the_poll_goes_to_the_new_topic(self) -> None:
        new = {"tier": "SEAL-1", "topic": "new-topic"}
        p, l = self._poll_that_changes_bindings(
            [{"chat_id": "42", "text": "ciao"}],
            before={"telegram": {"42": {"tier": "SEAL-1", "topic": "old-topic"}}},
            after={"telegram": {"42": new}})
        with p, l, patch.object(channel_relay, "_relay_chat") as relay:
            await channel_relay.run_poll_cycle(1)
        self.assertEqual(new, relay.call_args.args[1])

    async def test_a_chat_bound_during_the_poll_gets_its_first_messages(self) -> None:
        p, l = self._poll_that_changes_bindings(
            [{"chat_id": "99", "text": "primo"}],
            before={"telegram": {"42": {"tier": "SEAL-1", "topic": "ops"}}},
            after={"telegram": {"42": {"tier": "SEAL-1", "topic": "ops"},
                                "99": {"tier": "SEAL-1", "topic": "new"}}})
        with p, l, patch.object(channel_relay, "_relay_chat") as relay:
            served = await channel_relay.run_poll_cycle(1)
        self.assertEqual(1, served)
        self.assertEqual("99", relay.call_args.args[0])


class TheLoopChecksBeforePollingTests(unittest.TestCase):
    """Il loop di `main` deve interrogare i binding PRIMA di chiamare il
    trasporto.

    Senza questo controllo il conflitto resta: `run_poll_cycle` blocca sul poll
    per venticinque secondi e solo dopo scopre che non c'era niente da fare — e
    nel frattempo ha terminato il long-poll dell'altra istanza.
    """

    def test_the_loop_consults_the_bindings_first(self) -> None:
        import inspect
        from .. import main
        src = inspect.getsource(main.startup) if hasattr(main, "startup") else ""
        if not src:
            # il loop vive dentro l'handler di startup: si cerca nel modulo
            src = inspect.getsource(main)
        i_check = src.find("if not _cb.any_binding()")
        i_poll = src.find("run_poll_cycle(timeout)")
        self.assertGreater(i_check, -1, "il loop non consulta i binding")
        self.assertGreater(i_poll, i_check,
                           "il poll avviene PRIMA del controllo: il conflitto resta")


if __name__ == "__main__":
    unittest.main()
