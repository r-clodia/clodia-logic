"""`_watch_report` sveglia il guardiano anche a modalità globale spenta, se
sysadmin è già partecipante del topic dove l'anomalia è scattata.

Davide, 9 set 2026: prima l'unico modo di accendere la diagnostica era il
flag globale `CLODIA_DEBUG_MODE` (`debug_watch.enabled()`), che la espone
ovunque; aggiungere sysadmin ai `participants` di UN topic non aveva alcun
effetto — l'osservabilità restava tutto-o-niente. Chi compone il team di un
canale e ci mette sysadmin sta già scegliendo la diagnostica per quel
canale.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from .. import debug_watch as dw
from . import channels


class _Spec:
    def __init__(self, name):
        self.name = name


class WatchReportTopicPresenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        dw.reset_dedup()
        self.turns: list[tuple] = []

    def _patches(self, participants, *, globale=False, seal_ok=True):
        async def open_topic(tier, name):
            return {"meta": {"tier": "SEAL-1", "participants": participants}}

        async def start_turn(tier, name, tier_real, spec, principal, text, kind, **_kw):
            self.turns.append((spec.name, kind))
            return True

        return (
            patch.object(dw, "enabled", lambda: globale),
            patch.object(channels.topics_client, "async_open_topic", open_topic),
            patch.object(channels.registry, "get_by_name",
                         lambda n: _Spec("sysadmin") if n == "sysadmin" else None),
            patch.object(channels, "_provider_seal_ok", lambda *_a: seal_ok),
            patch.object(channels, "_start_turn", start_turn),
            patch.object(channels, "_max_delegation_hops", lambda: 5),
        )

    async def _run(self, participants, **kw):
        ps = self._patches(participants, **kw)
        for p in ps:
            p.start()
        try:
            await channels._watch_report(
                "SEAL-1", "acme", "turn_failed", "avvocato", "il turno è morto")
        finally:
            for p in ps:
                p.stop()

    async def test_global_off_but_watcher_is_a_participant_still_wakes_it(self):
        await self._run(participants=["sysadmin", "avvocato"], globale=False)
        self.assertEqual([("sysadmin", "debug")], self.turns)

    async def test_global_off_and_watcher_absent_stays_silent(self):
        await self._run(participants=["avvocato"], globale=False)
        self.assertEqual([], self.turns)

    async def test_global_on_wakes_it_regardless_of_participants(self):
        """Non-regressione: il flag globale resta un secondo modo di
        accenderla, non sostituito dalla presenza."""
        await self._run(participants=["avvocato"], globale=True)
        self.assertEqual([("sysadmin", "debug")], self.turns)

    async def test_the_topic_is_fetched_only_once(self):
        """Prima esisteva un fetch nel guard e uno per tier_real: due
        chiamate per la stessa anomalia sarebbero il costo raddoppiato che
        la presenza-come-trigger avrebbe potuto introdurre senza accorgersene."""
        chiamate = []

        async def open_topic(tier, name):
            chiamate.append((tier, name))
            return {"meta": {"tier": "SEAL-1", "participants": ["sysadmin"]}}

        async def start_turn(*_a, **_k):
            return True

        with patch.object(dw, "enabled", lambda: False), \
             patch.object(channels.topics_client, "async_open_topic", open_topic), \
             patch.object(channels.registry, "get_by_name", lambda n: _Spec("sysadmin")), \
             patch.object(channels, "_provider_seal_ok", lambda *_a: True), \
             patch.object(channels, "_start_turn", start_turn), \
             patch.object(channels, "_max_delegation_hops", lambda: 5):
            await channels._watch_report(
                "SEAL-1", "acme", "turn_failed", "avvocato", "il turno è morto")
        self.assertEqual(1, len(chiamate))

    async def test_a_topic_that_cannot_be_opened_stays_silent_not_raises(self):
        """Best-effort: la diagnostica non deve poter rompere il turno che
        stava già andando male."""
        async def boom(tier, name):
            raise RuntimeError("gateway muto")

        with patch.object(dw, "enabled", lambda: False), \
             patch.object(channels.topics_client, "async_open_topic", boom):
            await channels._watch_report(
                "SEAL-1", "acme", "turn_failed", "avvocato", "il turno è morto")
        # nessuna eccezione propagata: il test stesso è l'asserzione

    async def test_clearance_still_applies_on_the_presence_path(self):
        """La presenza non aggira la clearance: se sysadmin non è idoneo al
        tier reale, resta solo registrazione (stessa regola del percorso
        globale)."""
        await self._run(participants=["sysadmin"], globale=False, seal_ok=False)
        self.assertEqual([], self.turns)


if __name__ == "__main__":
    unittest.main()
