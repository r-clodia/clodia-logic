"""Un obiettivo fermo torna a bussare (clodia-platform#457).

«If Clodia itself fails for a system outage then there MUST be some background
process that checks "is_the_goal_reached == False AND are_agents_working_on_it
== FALSE" and trigger Clodia with a reminder» — la issue, alla lettera.

Il difetto che questo copre è invisibile: l'orchestratore muore a metà (crash,
riavvio, turno ucciso da un timeout), il goal resta scritto nel meta, e la
stanza tace. Il silenzio somiglia al lavoro in corso, quindi nessuno se ne
accorge finché non passa qualcuno a chiedere.

La regola è una funzione PURA sulle righe della lista topic: si prova senza
gateway e senza postare niente in una stanza vera.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from . import goal_watch

_ORA = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _riga(**kw):
    base = {
        "tier": "SEAL-1",
        "name": "progetto",
        "status": "active",
        "updated_at": (_ORA - timedelta(hours=3)).isoformat(),
        "goal": {"text": "Portare il sito in produzione", "state": "in-progress"},
    }
    base.update(kw)
    return base


def _fermi(righe, **kw):
    kw.setdefault("now", _ORA)
    kw.setdefault("fermo_da_minuti", 45)
    return goal_watch.obiettivi_fermi(righe, **kw)


class SelezioneTests(unittest.TestCase):
    def test_an_open_goal_with_nobody_working_is_picked_up(self) -> None:
        """Il caso della issue: obiettivo non raggiunto, nessuno al lavoro."""
        fermi = _fermi([_riga()])
        self.assertEqual(len(fermi), 1)
        self.assertEqual(fermi[0]["fermo_da_minuti"], 180)

    def test_a_channel_that_moved_recently_is_not_stale(self) -> None:
        """`updated_at` si muove a ogni messaggio: un canale che lavora non è
        mai fermo — ed è il motivo per cui non serve un timestamp nuovo."""
        self.assertEqual(_fermi([_riga(updated_at=(_ORA - timedelta(minutes=5)).isoformat())]), [])

    def test_an_agent_at_work_right_now_is_not_stale(self) -> None:
        """Un turno lungo che pensa senza scrivere non deve essere scambiato per
        un turno morto: il falso positivo qui lancia un SECONDO orchestratore
        sullo stesso piano."""
        self.assertEqual(_fermi([_riga()], occupato=lambda t, n: True), [])

    def test_states_waiting_for_the_owner_are_left_alone(self) -> None:
        """`strategy-review` e `claimed-done` aspettano una decisione umana:
        insistere lì sveglierebbe chi non ha niente da fare."""
        for stato in ("strategy-review", "claimed-done", "done"):
            with self.subTest(stato=stato):
                riga = _riga(goal={"text": "x", "state": stato})
                self.assertEqual(_fermi([riga]), [])

    def test_no_goal_no_reminder(self) -> None:
        self.assertEqual(_fermi([_riga(goal=None)]), [])
        self.assertEqual(_fermi([_riga(goal={"text": "  ", "state": "pinned"})]), [])

    def test_archived_or_closed_rooms_are_not_the_watchdog_business(self) -> None:
        """L'obiettivo è appeso a una stanza non più in esercizio: riaprirla a
        forza non è una ripresa, è rumore."""
        for stato in ("archived", "done"):
            with self.subTest(status=stato):
                self.assertEqual(_fermi([_riga(status=stato)]), [])

    def test_a_missing_or_broken_timestamp_does_not_trigger(self) -> None:
        """Senza sapere da quanto è fermo non si può dire che lo sia: meglio
        tacere che svegliare tutti a ogni tick."""
        self.assertEqual(_fermi([_riga(updated_at=None)]), [])
        self.assertEqual(_fermi([_riga(updated_at="ieri")]), [])

    def test_naive_timestamps_are_read_as_utc(self) -> None:
        """Il gateway scrive ISO con zona, ma un meta vecchio può non averla:
        senza questo il confronto esplode e la sorveglianza salta il canale."""
        riga = _riga(updated_at="2026-09-30T09:00:00")
        self.assertEqual(len(_fermi([riga])), 1)


class PromemoriaTests(unittest.TestCase):
    """Chi riceve il promemoria può essere uno spawn nuovo che non ha mai visto
    questa storia: deve poter ripartire da lì."""

    def test_the_reminder_says_what_to_do_next(self) -> None:
        voce = {"tier": "SEAL-1", "name": "p", "fermo_da_minuti": 180,
                "goal": {"text": "Andare live", "state": "in-progress",
                         "strategy_path": "local/goals/s.md"}}
        t = goal_watch.promemoria(voce)
        self.assertIn("Andare live", t)
        self.assertIn("180", t)
        self.assertIn("local/goals/s.md", t)
        # La via d'uscita onesta: se è già fatto, dichiaralo.
        self.assertIn("claimed-done", t)

    def test_a_goal_without_a_strategy_is_asked_for_one(self) -> None:
        voce = {"tier": "SEAL-1", "name": "p", "fermo_da_minuti": 60,
                "goal": {"text": "Andare live", "state": "pinned"}}
        self.assertIn("strategy-review", goal_watch.promemoria(voce))


class TickTests(unittest.IsolatedAsyncioTestCase):
    _META = {"owner": "davide", "contact_agent": "clodia",
             "participants": {"davide": "owner", "clodia": "contributor"}}

    async def _tick(self, righe, meta=None, apri_esplode=False):
        from . import channels
        apri = AsyncMock(side_effect=RuntimeError("giù")) if apri_esplode else \
            AsyncMock(return_value={"meta": meta or self._META})
        with patch.object(goal_watch, "__name__", goal_watch.__name__), \
             patch("server.api.topics_client.async_list_topics",
                   new=AsyncMock(return_value=righe)), \
             patch("server.api.topics_client.async_open_topic", new=apri), \
             patch.object(channels, "_qualcuno_al_lavoro", return_value=False), \
             patch("server.api.channels.post_channel_message",
                   new=AsyncMock(return_value={"posted": True})) as posta:
            res = await goal_watch.tick()
        return res, posta

    async def test_the_stale_goal_gets_the_orchestrator_mentioned(self) -> None:
        res, posta = await self._tick([_riga(updated_at="2026-01-01T00:00:00+00:00")])
        self.assertEqual(res["risvegliati"], ["SEAL-1/progetto"])
        testo = posta.await_args.args[2]
        self.assertTrue(testo.startswith("@clodia "))
        self.assertTrue(posta.await_args.kwargs["skip_if_busy"])

    async def test_no_orchestrator_in_the_room_no_noise(self) -> None:
        """Insistere ogni tick riempirebbe la stanza di promemoria che nessuno
        può raccogliere."""
        meta = {"owner": "davide", "contact_agent": "clodia", "participants": {"davide": "owner"}}
        res, posta = await self._tick([_riga(updated_at="2026-01-01T00:00:00+00:00")], meta=meta)
        posta.assert_not_awaited()
        self.assertEqual(res["risvegliati"], [])

    async def test_one_broken_channel_does_not_stop_the_sweep(self) -> None:
        """Il watchdog serve proprio quando qualcosa è rotto: se il primo canale
        che non si apre interrompesse il giro, mancherebbe nel momento in cui
        deve esserci."""
        res, _ = await self._tick([_riga(updated_at="2026-01-01T00:00:00+00:00")],
                                  apri_esplode=True)
        self.assertEqual(res["risvegliati"], [])
        self.assertEqual(res["esaminati"], 1)

    async def test_an_unreachable_gateway_is_not_an_exception(self) -> None:
        with patch("server.api.topics_client.async_list_topics",
                   new=AsyncMock(side_effect=RuntimeError("gateway giù"))):
            res = await goal_watch.tick()
        self.assertEqual(res, {"esaminati": 0, "risvegliati": []})


class SpegnimentoTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabling_the_watchdog_says_so(self) -> None:
        """Una sorveglianza spenta in silenzio è peggio che non averla: chi
        guarda i log deve poter sapere perché nessuno riprende gli obiettivi."""
        with patch.dict("os.environ", {"CLODIA_GOAL_STALE_MIN": "0"}), \
             self.assertLogs("agent-server.api.goal_watch", level="WARNING") as log:
            await goal_watch.loop()  # ritorna subito, senza entrare nel ciclo
        self.assertTrue(any("DISABILITATO" in r for r in log.output))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
