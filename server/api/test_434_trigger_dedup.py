"""Lo stesso trigger servito due volte è lo stesso lavoro fatto due volte.

clodia-logic#434. `trigger/internal` è fire-and-forget: chi la chiama non riceve
l'esito del turno, quindi un retry — o un loop di retry rotto lato proxy —
ripresenta lo STESSO testo pochi secondi dopo. Nei log dell'issue: 5 chiamate in
3m10s sullo stesso canale con lo stesso body.

Ogni chiamata faceva ripartire un turno a freddo che rilegge la stessa storia e
ri-decide le stesse delegazioni. Il *testo* finale del turno rigiocato viene
correttamente soppresso a valle (`risposta finale ... soppressa: N messaggi gia'
nel canale`), ma le `@menzioni` no: ognuna spawna un nuovo turno, e due spawn
dello stesso seed rivendicano la stessa issue prima di potersi leggere a
vicenda. È l'asimmetria che ha prodotto i doppioni su #420, #329 e #347.

Il dedup del testo non poteva chiuderla: arriva DOPO che il turno ha già speso
token, chiamato tool e postato. La guardia deve stare sulla porta, prima che il
turno parta — ed è questo che i test qui sotto fissano.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channels as ch


def _invecchia(secondi: float) -> None:
    """Fa invecchiare le voci già registrate, invece di spostare l'orologio.

    Patchare `time.monotonic` funzionerebbe, ma quel modulo è condiviso: lo
    legge anche l'event loop, che si mette a segnalare callback «lente 91
    secondi». Qui l'unica cosa che deve invecchiare è la finestra.
    """
    for chiave in list(ch._TRIGGERED):
        ch._TRIGGERED[chiave] -= secondi


class TriggerDedupTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        # La finestra è stato di modulo: senza questo, l'ordine dei test
        # deciderebbe il risultato.
        ch._TRIGGERED.clear()
        self.addCleanup(ch._TRIGGERED.clear)

    async def _trigger(self, text: str, by: str = "clodia") -> tuple[dict, list]:
        """Una chiamata alla porta. Ritorna la risposta e i turni avviati."""
        meta = {"owner": "davide", "participants": [by], "tier": "SEAL-1"}
        avviati: list = []

        def _fake_turn(tier, name, meta, **kw):
            # registrato alla CHIAMATA: `_spawn_bg` è finto e chiude la
            # coroutine senza eseguirla (stessa tecnica di
            # test_proxy_is_not_human).
            avviati.append(kw)

            async def _noop():
                return ("clodia", "ok")
            return _noop()

        async def _body():
            return {"text": text, "by": by}

        req = type("R", (), {})()
        req.json = _body

        with patch.object(ch.topics_client, "open_topic", return_value={"meta": meta}), \
             patch.object(ch, "_principal_from_request", return_value=by), \
             patch.object(ch, "_spawn_bg", side_effect=lambda coro: coro.close()), \
             patch.object(ch, "run_topic_turn", new=_fake_turn):
            out = await ch.channel_trigger_internal("SEAL-1", "software-house", req)
        return out, avviati

    async def test_the_same_trigger_twice_starts_one_turn(self) -> None:
        """Il difetto della #434, nella sua forma più corta."""
        primo, avviati_1 = await self._trigger("@fullstack-dev prendi la #434")
        secondo, avviati_2 = await self._trigger("@fullstack-dev prendi la #434")

        self.assertTrue(primo["triggered"])
        self.assertEqual(len(avviati_1), 1)
        self.assertFalse(secondo["triggered"])
        self.assertTrue(secondo.get("duplicate"))
        self.assertEqual(avviati_2, [], "il turno replica non deve nemmeno partire")

    async def test_a_different_text_is_not_a_replica(self) -> None:
        """La guardia è sul doppione, non sul canale: un lavoro nuovo passa."""
        await self._trigger("@fullstack-dev prendi la #434")
        out, avviati = await self._trigger("@fullstack-dev anzi prendi la #419")
        self.assertTrue(out["triggered"])
        self.assertEqual(len(avviati), 1)

    async def test_a_different_caller_is_not_a_replica(self) -> None:
        await self._trigger("stessa frase", by="clodia")
        out, avviati = await self._trigger("stessa frase", by="sysadmin")
        self.assertTrue(out["triggered"])
        self.assertEqual(len(avviati), 1)

    async def test_after_the_window_the_same_text_passes(self) -> None:
        """Soppressione a tempo, non per sempre: la stessa richiesta un'ora dopo
        è una richiesta, non un retry. Senza scadenza la guardia diventerebbe un
        canale che smette di rispondere a chi ripete la domanda."""
        await self._trigger("ripeti")
        _invecchia(ch._DEFAULT_TRIGGER_DEDUP_S + 1)
        out, avviati = await self._trigger("ripeti")
        self.assertTrue(out["triggered"])
        self.assertEqual(len(avviati), 1)

    async def test_the_window_can_be_switched_off(self) -> None:
        """La valvola: rimettere in moto un canale senza un deploy."""
        with patch.dict("os.environ", {"CLODIA_TRIGGER_DEDUP_S": "0"}):
            await self._trigger("martella")
            out, avviati = await self._trigger("martella")
        self.assertTrue(out["triggered"])
        self.assertEqual(len(avviati), 1)


class TriggerDedupWindowTests(unittest.TestCase):
    """Un trigger non deve morire per una variabile d'ambiente scritta male."""

    def test_default_when_unset(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("CLODIA_TRIGGER_DEDUP_S", None)
            self.assertEqual(ch._trigger_dedup_window(), ch._DEFAULT_TRIGGER_DEDUP_S)

    def test_garbage_falls_back_to_the_default(self) -> None:
        with patch.dict("os.environ", {"CLODIA_TRIGGER_DEDUP_S": "presto"}):
            self.assertEqual(ch._trigger_dedup_window(), ch._DEFAULT_TRIGGER_DEDUP_S)

    def test_a_negative_window_falls_back_to_the_default(self) -> None:
        """Negativo non è «spento» (quello è `0`): è illeggibile."""
        with patch.dict("os.environ", {"CLODIA_TRIGGER_DEDUP_S": "-5"}):
            self.assertEqual(ch._trigger_dedup_window(), ch._DEFAULT_TRIGGER_DEDUP_S)


class TriggerDedupMemoryTests(unittest.TestCase):
    """La finestra è in memoria: non deve crescere senza limite."""

    def setUp(self) -> None:
        ch._TRIGGERED.clear()
        self.addCleanup(ch._TRIGGERED.clear)

    def test_the_window_is_bounded(self) -> None:
        for i in range(ch._TRIGGERED_MAX * 2):
            ch._first_trigger("SEAL-1", "ch", "clodia", f"testo {i}")
        self.assertLessEqual(len(ch._TRIGGERED), ch._TRIGGERED_MAX)

    def test_expired_entries_are_dropped(self) -> None:
        ch._first_trigger("SEAL-1", "ch", "clodia", "vecchio")
        _invecchia(ch._DEFAULT_TRIGGER_DEDUP_S + 1)
        ch._first_trigger("SEAL-1", "ch", "clodia", "nuovo")
        self.assertEqual(len(ch._TRIGGERED), 1,
                         "la voce scaduta doveva uscire, non accumularsi")


if __name__ == "__main__":
    unittest.main()
