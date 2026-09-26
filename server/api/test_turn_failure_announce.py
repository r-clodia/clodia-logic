"""Un turno morto si dice nel canale, sempre.

Prima esisteva solo `_watch_report`, dietro `debug_watch.enabled()`: con la
diagnostica spenta il turno moriva, il log lo sapeva e la stanza restava muta.
Chi aveva scritto vedeva un agente che non risponde, e la prima ipotesi non è
mai «è andato in crash» — il 16 ago 2026 è costato mezza giornata.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channels


class _Spec:
    def __init__(self, name):
        self.name = name


class AnnounceFailureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.posted: list[tuple] = []
        self.turns: list[tuple] = []

    def _patches(self, watcher=_Spec("sysadmin"), seal_ok=True):
        def post(tier, name, author, text, kind="human", **_kw):
            self.posted.append((author, text, kind))
            return {"id": "1", "author": author, "text": text, "kind": kind, "ts": "1"}

        async def start_turn(tier, name, tier_real, spec, principal, text, kind, **_kw):
            self.turns.append((spec.name, kind))
            return True

        async def channel_message(*_a, **_k):
            return None

        return (
            patch.object(channels.topics_client, "post_message", post),
            patch.object(channels.topics_client, "open_topic",
                         lambda *_a, **_k: {"meta": {"tier": "SEAL-1"}}),
            patch.object(channels.registry, "get_by_name",
                         lambda n: watcher if n == "sysadmin" else None),
            patch.object(channels, "_provider_seal_ok", lambda *_a: seal_ok),
            patch.object(channels, "_start_turn", start_turn),
            patch.object(channels, "_channel_message", channel_message),
            patch.object(channels, "_topic_title", lambda *_a: "T"),
        )

    async def _run(self, responder, **kw):
        ps = self._patches(**kw)
        for p in ps:
            p.start()
        try:
            await channels._announce_failure(
                "SEAL-1", "acme", responder, RuntimeError("codex exit 1"))
        finally:
            for p in ps:
                p.stop()

    async def test_the_channel_says_it_and_calls_sysadmin(self):
        await self._run("fullstack-dev#1")
        self.assertEqual(len(self.posted), 1, "il canale è rimasto muto")
        _autore, testo, _kind = self.posted[0]
        self.assertIn("fullstack-dev#1", testo)
        self.assertIn("codex exit 1", testo, "l'errore va detto, non riassunto")
        self.assertIn("@sysadmin", testo)
        self.assertEqual([t[0] for t in self.turns], ["sysadmin"])

    async def test_when_the_watcher_itself_fails_it_is_not_called_again(self):
        """Chiamarlo lo farebbe cadere sullo stesso errore, e ogni caduta ne
        chiamerebbe un'altra."""
        await self._run("sysadmin")
        self.assertEqual(len(self.posted), 1, "il messaggio serve comunque")
        # `@sysadmin` compare eccome: è il nome di chi è caduto. Ciò che non
        # deve esserci è la RICHIESTA di diagnosi, e soprattutto la chiamata.
        self.assertNotIn("puoi guardare", self.posted[0][1])
        self.assertIn("il guasto riguarda il guardiano stesso", self.posted[0][1])
        self.assertEqual(self.turns, [], "il guardiano è stato richiamato su sé stesso")

    async def test_a_tier_the_watcher_cannot_enter_still_gets_the_message(self):
        await self._run("fullstack-dev", seal_ok=False)
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(self.turns, [])

    async def test_no_watcher_registered_is_not_an_error(self):
        await self._run("fullstack-dev", watcher=None)
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(self.turns, [])

    async def test_an_exception_with_a_readable_note_is_shown_as_such(self):
        """clodia-platform#397 (punto 3) · quando chi ha sollevato l'eccezione
        ha già scritto la diagnosi, la stanza legge quella.

        Il caso vero è la sessione morta e ricreata: `repr` diceva soltanto
        `CLIConnectionError('Cannot write to terminated process (exit code:
        143)')`, cioè né che il messaggio era andato perso né che bastava
        rimandarlo. L'annunciatore non può dedurlo — glielo dice l'eccezione.
        """
        class _Parlante(RuntimeError):
            nota_utente = ("La sessione era terminata in modo inatteso e il "
                           "messaggio non è stato elaborato: è stata ricreata, "
                           "basta rimandarlo.\nDettaglio: exit code 143.")

        ps = self._patches()
        for p in ps:
            p.start()
        try:
            await channels._announce_failure("SEAL-1", "acme", "clodia#1",
                                             _Parlante("qualunque cosa"))
        finally:
            for p in ps:
                p.stop()
        testo = self.posted[0][1]
        self.assertIn("basta rimandarlo", testo)
        self.assertIn("exit code 143", testo)
        self.assertNotIn("_Parlante(", testo, "il repr grezzo era il difetto")

    async def test_without_a_note_nothing_changes(self):
        """Il `repr` resta il default: è l'unica cosa che c'è, per la stragrande
        maggioranza dei guasti."""
        await self._run("fullstack-dev#1")
        self.assertIn("RuntimeError('codex exit 1')", self.posted[0][1])

    async def test_announcing_a_failure_never_raises(self):
        """Annunciare un guasto non deve poterne produrre un secondo."""
        def boom(*_a, **_k):
            raise RuntimeError("topic store giù")

        with patch.object(channels.topics_client, "post_message", boom):
            await channels._announce_failure("SEAL-1", "acme", "x", RuntimeError("y"))


if __name__ == "__main__":
    unittest.main()
