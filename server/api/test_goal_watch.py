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

The clock is the channel's ACTIVITY (last message, last turn end), not the
topic list's `updated_at`, which on the gateway does not move with messages;
and reminders are capped per goal, with backoff, so the watcher can never
drive unbounded LLM turns.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from . import goal_watch

_ORA = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


_K = ("SEAL-1", "progetto")


def _riga(**kw):
    base = {
        "tier": "SEAL-1",
        "name": "progetto",
        "status": "active",
        # Recent on purpose: the list's updated_at must NOT be the clock.
        "updated_at": _ORA.isoformat(),
        "goal": {"text": "Portare il sito in produzione", "state": "in-progress"},
    }
    base.update(kw)
    return base


def _fermi(righe, *, attivo=_ORA - timedelta(hours=3), **kw):
    kw.setdefault("now", _ORA)
    kw.setdefault("fermo_da_minuti", 45)
    kw.setdefault("attivita", {_K: attivo.isoformat() if isinstance(attivo, datetime) else attivo})
    return goal_watch.obiettivi_fermi(righe, **kw)


class SelezioneTests(unittest.TestCase):
    def test_an_open_goal_with_nobody_working_is_picked_up(self) -> None:
        """Il caso della issue: obiettivo non raggiunto, nessuno al lavoro."""
        fermi = _fermi([_riga()])
        self.assertEqual(len(fermi), 1)
        self.assertEqual(fermi[0]["fermo_da_minuti"], 180)

    def test_a_channel_that_moved_recently_is_not_stale(self) -> None:
        """A recent message or turn end keeps the channel alive."""
        self.assertEqual(_fermi([_riga()], attivo=_ORA - timedelta(minutes=5)), [])

    def test_the_topic_list_updated_at_is_not_the_clock(self) -> None:
        """The review blocker: on the gateway `updated_at` is
        max(meta, summary, AGENTS.md) and does not move with messages. An old
        updated_at with a lively channel is NOT stale; a fresh updated_at with
        a silent channel IS."""
        vecchio = (_ORA - timedelta(days=3)).isoformat()
        self.assertEqual(_fermi([_riga(updated_at=vecchio)],
                                attivo=_ORA - timedelta(minutes=2)), [])
        self.assertEqual(len(_fermi([_riga(updated_at=_ORA.isoformat())],
                                    attivo=_ORA - timedelta(hours=2))), 1)

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
        self.assertEqual(_fermi([_riga()], attivo=None), [])
        self.assertEqual(_fermi([_riga()], attivo="ieri"), [])
        self.assertEqual(_fermi([_riga()], attivita={}), [])

    def test_naive_timestamps_are_read_as_utc(self) -> None:
        """Il gateway scrive ISO con zona, ma un meta vecchio può non averla:
        senza questo il confronto esplode e la sorveglianza salta il canale."""
        self.assertEqual(len(_fermi([_riga()], attivo="2026-09-30T09:00:00")), 1)


class CapAndBackoffTests(unittest.TestCase):
    """The watcher must never be a loop: at most N reminders per goal without
    new human activity or goal progress, each one after a longer silence."""

    def _st(self, count, ping_min_ago, goal=None):
        g = goal or _riga()["goal"]
        return {_K: {"firma": goal_watch.firma(g), "count": count,
                     "last_ping": (_ORA - timedelta(minutes=ping_min_ago)).isoformat()}}

    def test_the_wake_up_resets_the_clock(self) -> None:
        # Channel silent for 3 h, but the reminder went out 10 min ago.
        self.assertEqual(_fermi([_riga()], solleciti=self._st(1, 10)), [])

    def test_backoff_doubles_the_silence_required(self) -> None:
        # After 1 reminder: 90 min needed. 60 is not enough, 100 is.
        self.assertEqual(_fermi([_riga()], solleciti=self._st(1, 60)), [])
        fermi = _fermi([_riga()], solleciti=self._st(1, 100))
        self.assertEqual([f["promemoria_n"] for f in fermi], [2])

    def test_no_reminder_beyond_the_cap(self) -> None:
        self.assertEqual(_fermi([_riga()], solleciti=self._st(3, 10_000),
                                max_promemoria=3), [])

    def test_a_goal_that_moved_starts_a_new_series(self) -> None:
        vecchio = {"text": "Portare il sito in produzione", "state": "pinned"}
        fermi = _fermi([_riga()], solleciti=self._st(3, 10_000, goal=vecchio),
                       max_promemoria=3)
        self.assertEqual([f["promemoria_n"] for f in fermi], [1])


class AttivitaDaMessaggiTests(unittest.TestCase):
    def test_last_message_and_last_human_message(self) -> None:
        msgs = [
            {"ts": "2026-09-30T09:00:00+00:00", "author": "davide", "kind": "human", "text": "vai"},
            {"ts": "2026-09-30T10:00:00+00:00", "author": "clodia", "kind": "ai", "text": "ok"},
            {"ts": "2026-09-30T11:00:00+00:00", "author": "system", "kind": "system",
             "text": f"@clodia {goal_watch.MARCA}, ..."},
        ]
        ultimo, umano = goal_watch.attivita_da_messaggi(msgs)
        self.assertEqual(ultimo.hour, 11)
        self.assertEqual(umano.hour, 9)


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

    def setUp(self) -> None:
        goal_watch._SOLLECITI.clear()
        self.addCleanup(goal_watch._SOLLECITI.clear)
        self.messaggi: list[dict] = []
        self.turno = None

    def _msg(self, when, author="davide", kind="human", text="ciao"):
        self.messaggi.append({"ts": when.isoformat(), "author": author, "kind": kind,
                              "text": text})

    async def _tick(self, righe, meta=None, apri_esplode=False, now=_ORA):
        from . import channels
        apri = AsyncMock(side_effect=RuntimeError("giù")) if apri_esplode else \
            AsyncMock(return_value={"meta": meta or self._META})

        async def posta_e_registra(tier, name, text, author, **kw):
            # The reminder is a channel message: it lands in the history.
            self.messaggi.append({"ts": self._now.isoformat(), "author": author,
                                  "kind": kw.get("kind", "human"), "text": text})
            return {"posted": True}
        self._now = now
        with patch("server.api.topics_client.async_list_topics",
                   new=AsyncMock(return_value=righe)), \
             patch("server.api.topics_client.async_open_topic", new=apri), \
             patch("server.api.topics_client.async_list_messages",
                   new=AsyncMock(side_effect=lambda t, n, limit=50: list(self.messaggi))), \
             patch.object(channels, "_qualcuno_al_lavoro", return_value=False), \
             patch.object(channels, "ultimo_turno_finito", lambda t, n: self.turno), \
             patch("server.api.channels.post_channel_message",
                   new=AsyncMock(side_effect=posta_e_registra)) as posta:
            res = await goal_watch.tick(now=now)
        return res, posta

    async def test_the_stale_goal_gets_the_orchestrator_mentioned(self) -> None:
        self._msg(_ORA - timedelta(hours=2))
        res, posta = await self._tick([_riga()])
        self.assertEqual(res["risvegliati"], ["SEAL-1/progetto"])
        testo = posta.await_args.args[2]
        self.assertTrue(testo.startswith("@clodia "))
        self.assertTrue(posta.await_args.kwargs["skip_if_busy"])

    async def test_a_recent_message_resets_the_clock(self) -> None:
        """The blocker, end to end: a channel whose meta is old but whose
        messages are recent is not pinged."""
        self._msg(_ORA - timedelta(minutes=3), author="clodia", kind="ai")
        res, posta = await self._tick([_riga(updated_at="2026-01-01T00:00:00+00:00")])
        posta.assert_not_awaited()
        self.assertEqual(res["risvegliati"], [])

    async def test_a_recent_turn_end_resets_the_clock(self) -> None:
        self._msg(_ORA - timedelta(hours=5))
        self.turno = _ORA - timedelta(minutes=10)
        _res, posta = await self._tick([_riga()])
        posta.assert_not_awaited()

    async def test_no_repeated_pings_without_new_activity_beyond_the_cap(self) -> None:
        """Ten-minute ticks over two days of silence (the agent answers each
        reminder, nobody human speaks): the pings stop at the cap."""
        self._msg(_ORA - timedelta(hours=2))
        pings = 0
        with patch.dict("os.environ", {"CLODIA_GOAL_MAX_REMINDERS": "3"}):
            for i in range(6 * 48):
                now = _ORA + timedelta(minutes=10 * i)
                res, _ = await self._tick([_riga()], now=now)
                if res["risvegliati"]:
                    pings += 1
                    # The orchestrator answers the reminder: not human activity.
                    self._msg(now + timedelta(seconds=30), author="clodia", kind="ai",
                              text="ci sono")
        self.assertEqual(pings, 3)

    async def test_the_wake_up_itself_resets_the_clock(self) -> None:
        self._msg(_ORA - timedelta(hours=2))
        r1, _ = await self._tick([_riga()], now=_ORA)
        r2, _ = await self._tick([_riga()], now=_ORA + timedelta(minutes=10))
        self.assertEqual((len(r1["risvegliati"]), len(r2["risvegliati"])), (1, 0))

    async def test_a_human_message_starts_a_new_series(self) -> None:
        self._msg(_ORA - timedelta(hours=12))
        goal_watch._SOLLECITI[_K] = {"firma": goal_watch.firma(_riga()["goal"]),
                                     "count": 3,
                                     "last_ping": (_ORA - timedelta(hours=10)).isoformat()}
        res, _ = await self._tick([_riga()])
        self.assertEqual(res["risvegliati"], [], "cap reached, nobody spoke")
        self._msg(_ORA - timedelta(hours=1))  # a person, after the last ping
        res, _ = await self._tick([_riga()])
        self.assertEqual(res["risvegliati"], ["SEAL-1/progetto"])

    async def test_no_orchestrator_in_the_room_no_noise(self) -> None:
        """Insistere ogni tick riempirebbe la stanza di promemoria che nessuno
        può raccogliere."""
        self._msg(_ORA - timedelta(hours=2))
        meta = {"owner": "davide", "contact_agent": "clodia", "participants": {"davide": "owner"}}
        res, posta = await self._tick([_riga()], meta=meta)
        posta.assert_not_awaited()
        self.assertEqual(res["risvegliati"], [])

    async def test_one_broken_channel_does_not_stop_the_sweep(self) -> None:
        """Il watchdog serve proprio quando qualcosa è rotto: se il primo canale
        che non si apre interrompesse il giro, mancherebbe nel momento in cui
        deve esserci."""
        self._msg(_ORA - timedelta(hours=2))
        res, _ = await self._tick([_riga()], apri_esplode=True)
        self.assertEqual(res["risvegliati"], [])
        self.assertEqual(res["esaminati"], 1)

    async def test_unreadable_messages_mean_no_reminder(self) -> None:
        from . import channels
        with patch("server.api.topics_client.async_list_topics",
                   new=AsyncMock(return_value=[_riga()])), \
             patch("server.api.topics_client.async_list_messages",
                   new=AsyncMock(side_effect=RuntimeError("giù"))), \
             patch.object(channels, "ultimo_turno_finito", lambda t, n: None), \
             patch("server.api.channels.post_channel_message", new=AsyncMock()) as posta:
            res = await goal_watch.tick(now=_ORA)
        posta.assert_not_awaited()
        self.assertEqual(res["risvegliati"], [])

    async def test_an_unreachable_gateway_is_not_an_exception(self) -> None:
        with patch("server.api.topics_client.async_list_topics",
                   new=AsyncMock(side_effect=RuntimeError("gateway giù"))):
            res = await goal_watch.tick()
        self.assertEqual(res, {"esaminati": 0, "risvegliati": []})


class TurnEndIsRecordedTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_and_post_response_records_the_turn_end(self) -> None:
        from . import channels
        chat = type("C", (), {"chat_id": "chan:SEAL-1:progetto:clodia"})()
        chat.send_user_message = AsyncMock(return_value="ok")

        async def noop(*_a, **_kw):
            return None
        channels._ULTIMO_TURNO.pop(_K, None)
        with patch.object(channels.topics_client, "async_list_messages", AsyncMock(return_value=[])), \
             patch.object(channels.topics_client, "async_post_message", AsyncMock()), \
             patch.object(channels, "_maybe_delegate", noop), \
             patch.object(channels, "_typing", noop), \
             patch.object(channels, "_channel_message", noop), \
             patch.object(channels, "_spawn_bg", lambda c: c.close()):
            await channels._run_and_post_response("SEAL-1", "progetto", "clodia", chat, "p")
        self.assertIsNotNone(channels.ultimo_turno_finito(*_K))


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
