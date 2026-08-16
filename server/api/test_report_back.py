"""Il turno finito torna a chi l'ha lanciato, senza che nessuno aspetti.

Un orchestratore che delega non può restare appeso al turno del delegato: il
modello è asincrono e un `await` metterebbe una chiamata bloccante dentro un
sistema a turni, con il chiamante fermo su qualcosa che — se il delegato muore —
non arriva mai. Il ritorno è quindi un evento, costruito sulla catena `origin`
che la piattaforma tiene già.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channels


class _Spec:
    def __init__(self, name, type_="bot"):
        self.name = name
        self.type = type_


class _Chat:
    def __init__(self, origin):
        self.origin = origin


CHAIN = ["human:davide", "agent:clodia", "agent:fullstack-dev"]


class CallerOfTests(unittest.TestCase):
    def test_the_caller_is_the_agent_before_the_executor(self):
        self.assertEqual(channels._caller_of(CHAIN, "fullstack-dev"), "clodia")

    def test_a_spawn_ordinal_does_not_confuse_it(self):
        self.assertEqual(channels._caller_of(CHAIN, "fullstack-dev#2"), "clodia")

    def test_a_turn_nobody_delegated_owes_nothing(self):
        self.assertIsNone(channels._caller_of(["human:davide", "agent:clodia"], "clodia"))
        self.assertIsNone(channels._caller_of(None, "clodia"))


class ReportBackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.turns: list[tuple] = []

    def _patches(self, participants=("clodia", "fullstack-dev"), spec=_Spec("clodia")):
        async def start_turn(tier, name, tier_real, s, principal, text, kind, **kw):
            self.turns.append((s.name, text, kw.get("hop")))
            return True

        return (
            patch.object(channels, "_start_turn", start_turn),
            patch.object(channels.registry, "get_by_name",
                         lambda n: spec if spec and n == spec.name else None),
            patch.object(channels.topics_client, "open_topic",
                         lambda *_a, **_k: {"meta": {"tier": "SEAL-1",
                                                     "participants": list(participants)}}),
            patch.object(channels, "_provider_seal_ok", lambda *_a: True),
        )

    async def _run(self, esito, hop=0, **kw):
        ps = self._patches(**kw)
        for p in ps:
            p.start()
        try:
            await channels._report_back("SEAL-1", "acme", "fullstack-dev",
                                        _Chat(CHAIN), esito, hop)
        finally:
            for p in ps:
                p.stop()

    async def test_the_orchestrator_is_woken_with_the_outcome(self):
        await self._run("PR aperta: clodia-logic#299")
        self.assertEqual(len(self.turns), 1)
        nome, testo, hop = self.turns[0]
        self.assertEqual(nome, "clodia")
        self.assertIn("PR aperta: clodia-logic#299", testo, "l'esito deve viaggiare")
        self.assertIn("turno concluso", testo)
        self.assertEqual(hop, 1)

    async def test_an_explicit_tag_is_not_doubled(self):
        """Se il delegato ha già taggato il chiamante, l'ha svegliato
        `_maybe_delegate`: due inneschi darebbero due turni per un evento solo."""
        await self._run("fatto, @clodia a te la prossima")
        self.assertEqual(self.turns, [])

    async def test_the_hop_limit_stops_the_ping_pong(self):
        await self._run("fatto", hop=channels._MAX_DELEGATION_HOPS)
        self.assertEqual(self.turns, [])

    async def test_a_caller_who_left_the_room_is_not_called_back(self):
        await self._run("fatto", participants=("fullstack-dev",))
        self.assertEqual(self.turns, [])

    async def test_a_human_caller_is_not_a_turn(self):
        """La catena può nominare una persona: una persona non si innesca."""
        await self._run("fatto", spec=_Spec("clodia", type_="human"))
        self.assertEqual(self.turns, [])

    async def test_reporting_back_never_breaks_the_turn(self):
        with patch.object(channels.registry, "get_by_name",
                          lambda _n: (_ for _ in ()).throw(RuntimeError("registry giù"))):
            await channels._report_back("SEAL-1", "acme", "fullstack-dev",
                                        _Chat(CHAIN), "fatto", 0)


if __name__ == "__main__":
    unittest.main()
