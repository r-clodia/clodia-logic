"""Un trasporto che non risponde si aspetta, non si martella (#539 §6).

Durante il rebuild del gateway del 14 ott 2026 il relay ha scritto **145** righe
«poll del trasporto telegram» in pochi secondi. Il poll non bloccava: falliva
subito, il ciclo rientrava subito, e il log — l'unica cosa che serviva in quel
momento — è diventato illeggibile.

I due fatti che questi test misurano:
1. il ciclo successivo a un fallimento NON richiama il trasporto (pausa), e
   l'attesa cresce invece di ripetersi uguale;
2. il guasto produce UNA riga di warning, non una per tentativo, e il rientro
   un'altra: un guasto è un evento, non un evento per tentativo.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channel_relay

_BINDINGS = {"telegram": {"42": {"tier": "SEAL-1", "name": "ops"}}}


class _Orologio:
    """Tempo finto: `run_poll_cycle` chiede l'ora al loop, qui la decido io."""

    def __init__(self) -> None:
        self.t = 1000.0

    def time(self) -> float:
        return self.t


class PollBackoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        channel_relay._POLL_BACKOFF.clear()
        self.addCleanup(channel_relay._POLL_BACKOFF.clear)
        self.clock = _Orologio()
        self.tentativi = 0

    def _ambiente(self, rotto: bool):
        async def _poll(timeout):
            self.tentativi += 1
            if rotto:
                raise RuntimeError("gateway irraggiungibile")
            return []

        adapter = type("A", (), {"poll": staticmethod(_poll)})()
        return (patch.object(channel_relay.cb, "load", return_value=_BINDINGS),
                patch.object(channel_relay.relay_adapters, "get", return_value=adapter),
                patch.object(channel_relay.asyncio, "get_event_loop",
                             return_value=self.clock),
                patch.object(channel_relay.asyncio, "sleep", self._sleep))

    async def _sleep(self, secondi):
        """Non dorme davvero: fa avanzare l'orologio finto e lo registra."""
        self.dormito = getattr(self, "dormito", [])
        self.dormito.append(secondi)

    async def _cycle(self, rotto: bool = True) -> int:
        a, b, c, d = self._ambiente(rotto)
        with a, b, c, d:
            return await channel_relay.run_poll_cycle(25)

    async def test_a_failed_transport_is_not_polled_again_immediately(self) -> None:
        await self._cycle()
        self.assertEqual(1, self.tentativi)
        await self._cycle()          # subito dopo: deve essere in pausa
        self.assertEqual(1, self.tentativi, "il trasporto è stato rimartellato")
        self.clock.t += 2.1          # scaduta la prima attesa
        await self._cycle()
        self.assertEqual(2, self.tentativi)

    async def test_the_wait_grows_instead_of_repeating(self) -> None:
        attese = []
        for _ in range(4):
            await self._cycle()
            attese.append(channel_relay._POLL_BACKOFF["telegram"]["next"] - self.clock.t)
            self.clock.t += attese[-1] + 0.01
        self.assertEqual([2.0, 4.0, 8.0, 16.0], attese)

    async def test_the_wait_has_a_ceiling(self) -> None:
        channel_relay._POLL_BACKOFF["telegram"] = {"fails": 20, "next": 0.0}
        await self._cycle()
        attesa = channel_relay._POLL_BACKOFF["telegram"]["next"] - self.clock.t
        self.assertEqual(channel_relay._POLL_BACKOFF_MAX, attesa)

    async def test_a_cycle_that_polled_nothing_still_waits(self) -> None:
        """Il ciclo in pausa non deve durare zero: è il giro a vuoto che il
        chiamante ripete all'infinito (il loop di `main` non dorme da sé)."""
        await self._cycle()
        self.dormito = []
        await self._cycle()
        self.assertTrue(self.dormito, "ciclo a vuoto senza attesa: hot loop")
        self.assertLessEqual(self.dormito[0], 25.0)

    async def test_one_line_per_outage_not_per_attempt(self) -> None:
        with self.assertLogs("agent-server.channel_relay", level="DEBUG") as log:
            for _ in range(6):
                await self._cycle()
                self.clock.t += 100.0     # ogni giro riprova davvero
        warning = [r for r in log.records if r.levelname == "WARNING"]
        self.assertEqual(1, len(warning), [r.getMessage() for r in log.records])
        self.assertEqual(6, self.tentativi)

    async def test_the_recovery_is_logged_once_and_clears_the_state(self) -> None:
        await self._cycle()
        self.clock.t += 100.0
        with self.assertLogs("agent-server.channel_relay", level="INFO") as log:
            await self._cycle(rotto=False)
        self.assertNotIn("telegram", channel_relay._POLL_BACKOFF)
        self.assertEqual(1, len([r for r in log.records if "rientrato" in r.getMessage()]))

    async def test_a_healthy_transport_logs_nothing_and_keeps_no_state(self) -> None:
        await self._cycle(rotto=False)
        self.assertEqual({}, channel_relay._POLL_BACKOFF)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
