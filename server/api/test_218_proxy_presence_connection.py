"""Una socket chiusa è un'assenza SAPUTA, non una scadenza da aspettare.

clodia-platform#218. Il battito che nasce dalla connessione c'è già (#326): lo
stream SSE tiene la presenza di un proxy per tutta la sua durata. Manca l'altra
metà della frase della issue — «a dropped stream means *not present*» — perché
la caduta non viene mai SCRITTA: nel `finally` si cancellava il task e nient'altro.

Misurato sul clone, prima del fix, con `_PROXY_BEAT_EVERY_S = 50` e
`presence.TTL_S = 150`:

    subito                        → here
    a +100s dall'ultimo battito   → here      ← la socket è chiusa
    a +160s                       → away

Cioè il posto resta occupato fino a due minuti e mezzo dopo che il ponte è
morto. È il difetto che #218 descrive («a seat held while that system is down is
worse than an empty seat»), ristretto da «per sempre» a «150 s» e ristretto per
SCADENZA, non per conoscenza: la differenza è che la scadenza non sa niente, e un
pallino che dice «sta leggendo» mentre non c'è nessuno è peggio di nessun pallino.

L'altra metà di questi controlli è il silenzio dopo una menzione. La piattaforma
non apre mai un turno su un proxy (A11, `test_proxy_is_not_a_responder_even_when_tagged`),
quindi `@clodia-primal ...` non produceva NULLA nella stanza: la ragione del
routing tornava nella risposta HTTP, che la webui non legge. Chi scrive resta ad
aspettare senza modo di sapere che sta aspettando.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from . import agents, channels, presence
from .test_channels import _a

CHI, TIER, STANZA = "clodia-primal", "SEAL-1", "acme"


class _ConDatadirTemporanea(unittest.TestCase):
    """`presence.json` sta nella datadir: qui se ne usa una usa-e-getta.

    `presence._path()` rilegge l'ambiente a ogni chiamata, quindi basta la
    variabile — nessuna patch sul modulo, e il file scritto è quello vero.
    """

    def setUp(self) -> None:
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = patch.dict(os.environ, {"CLODIA_DATA": d.name})
        p.start()
        self.addCleanup(p.stop)


class LaCadutaSiScriveTests(_ConDatadirTemporanea):
    def test_dropping_makes_the_absence_immediate(self) -> None:
        """Il controllo che era rosso: senza `drop` restava `here` per 150 s."""
        presence.touch(CHI, TIER, STANZA)
        self.assertEqual("here", presence.stato(CHI, TIER, STANZA))
        presence.drop(CHI, TIER, STANZA, anche_ovunque=True)
        self.assertEqual("away", presence.stato(CHI, TIER, STANZA))

    def test_the_room_key_goes_and_the_general_one_can_stay(self) -> None:
        """Due chiavi, due domande: `-` dice «è collegato da qualche parte» ed è
        condivisa fra le stanze. Toglierla mentre un altro stream è aperto
        racconterebbe assente chi è connesso."""
        presence.touch(CHI, TIER, STANZA)
        presence.drop(CHI, TIER, STANZA)
        d = presence._load()
        self.assertNotIn(f"{CHI}|{TIER}/{STANZA}", d)
        self.assertIn(f"{CHI}|{presence.OVUNQUE}", d)

    def test_dropping_the_general_key_too(self) -> None:
        presence.touch(CHI, TIER, STANZA)
        presence.drop(CHI, TIER, STANZA, anche_ovunque=True)
        self.assertEqual({}, presence._load())

    def test_dropping_what_was_never_there_is_not_an_error(self) -> None:
        """Uno stream che cade prima del primo battito è un caso normale, non
        un'eccezione da propagare dentro il `finally` di un generatore."""
        presence.drop("mai-visto", TIER, STANZA, anche_ovunque=True)
        self.assertEqual("away", presence.stato("mai-visto", TIER, STANZA))

    def test_it_does_not_touch_another_room_of_the_same_proxy(self) -> None:
        presence.touch(CHI, TIER, STANZA)
        presence.touch(CHI, TIER, "altra")
        presence.drop(CHI, TIER, STANZA)
        self.assertEqual("here", presence.stato(CHI, TIER, "altra"))


class DueStreamNonSiCancellanoTests(unittest.IsolatedAsyncioTestCase):
    """Il conteggio, e la ragione per cui non è un booleano.

    Una riconnessione si SOVRAPPONE alla connessione che sta cadendo: il ponte
    riapre lo stream e solo dopo il vecchio si chiude. Con un booleano quella
    chiusura cancellerebbe la presenza appena riaperta, e il pallino lampeggerebbe
    fra presente e assente — che sembra un guasto del ponte, cioè peggio di non
    mostrarlo affatto (è la stessa ragione per cui `_PROXY_BEAT_EVERY_S` sta sotto
    il TTL).
    """

    def setUp(self) -> None:
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = patch.dict(os.environ, {"CLODIA_DATA": d.name})
        p.start()
        self.addCleanup(p.stop)
        agents._stream_di_proxy.clear()
        self.addCleanup(agents._stream_di_proxy.clear)

    async def _apri(self, tier=TIER, name=STANZA):
        battito = agents._presenza_stream_aperto(CHI, tier, name)
        await asyncio.sleep(0.01)  # il primo battito parte dal task
        return battito

    async def test_the_first_close_does_not_wipe_the_second_stream(self) -> None:
        primo = await self._apri()
        secondo = await self._apri()
        self.assertEqual("here", presence.stato(CHI, TIER, STANZA))

        agents._presenza_stream_chiuso(CHI, TIER, STANZA, primo)
        self.assertEqual("here", presence.stato(CHI, TIER, STANZA),
                         "la chiusura di uno stream sovrapposto ha cancellato "
                         "la presenza dell'altro, ancora aperto")

        agents._presenza_stream_chiuso(CHI, TIER, STANZA, secondo)
        self.assertEqual("away", presence.stato(CHI, TIER, STANZA))

    async def test_closing_the_last_stream_stops_the_beat(self) -> None:
        battito = await self._apri()
        agents._presenza_stream_chiuso(CHI, TIER, STANZA, battito)
        with contextlib.suppress(asyncio.CancelledError):
            await battito
        self.assertTrue(battito.cancelled() or battito.done(),
                        "il battito sopravvive alla connessione: un ponte "
                        "terminato resterebbe «presente» per sempre")

    async def test_another_room_keeps_the_general_key(self) -> None:
        """`-` cade solo quando al proxy non resta NESSUNO stream aperto."""
        qui = await self._apri()
        await self._apri(name="altra")
        agents._presenza_stream_chiuso(CHI, TIER, STANZA, qui)
        self.assertIn(f"{CHI}|{presence.OVUNQUE}", presence._load())
        self.assertEqual("elsewhere", presence.stato(CHI, TIER, STANZA))

    async def test_the_bookkeeping_does_not_leak(self) -> None:
        """Una voce per stanza per sempre sarebbe una perdita di memoria lenta:
        la chiave sparisce con l'ultimo stream."""
        battito = await self._apri()
        agents._presenza_stream_chiuso(CHI, TIER, STANZA, battito)
        self.assertEqual({}, agents._stream_di_proxy)


class UnaMenzioneAUnProxyAssenteLoDiceTests(unittest.IsolatedAsyncioTestCase):
    """La stanza sa che non c'è nessuno in ascolto, invece di dedurlo dal silenzio.

    Stesso principio di `_announce_refusal` e `_announce_provider_inadeguato`, e
    la stessa forma: autore `system`, best-effort, la ragione in chiaro. «Un
    turno che non parte in silenzio è indistinguibile da un agente rotto» —
    qui non parte perché dall'altra parte non c'è nessuno, e il messaggio non è
    perso: resta nella storia, e il ponte lo ritrova al rientro.
    """

    def setUp(self) -> None:
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = patch.dict(os.environ, {"CLODIA_DATA": d.name})
        p.start()
        self.addCleanup(p.stop)
        self.agents = {
            "owner": _a("owner", "human", role="superadmin"),
            "worker": _a("worker", "normal", "P1", "2026-02-01T00:00:00Z"),
            CHI: _a(CHI, "proxy"),
        }
        self._orig_get = channels.registry.get_by_name
        self._orig_track = channels._track_routing_decision
        channels.registry.get_by_name = lambda n: self.agents.get(n)
        channels._track_routing_decision = lambda _payload: None
        self.addCleanup(self._ripristina)

    def _ripristina(self) -> None:
        channels.registry.get_by_name = self._orig_get
        channels._track_routing_decision = self._orig_track

    async def _post(self, testo: str, partecipanti=None):
        """Ritorna `(esito, [(autore, testo, kind), ...])` dei messaggi scritti."""
        scritti: list[tuple] = []

        def _post_message(tier, name, autore, testo, kind="human", **_k):
            scritti.append((autore, testo, kind))
            return {"id": str(len(scritti))}

        parts = partecipanti if partecipanti is not None else ["owner", "worker", CHI]
        with contextlib.ExitStack() as stack:
            for cm in (
                patch.object(channels, "_provider_seal_ok", return_value=True),
                patch.object(channels.topics_client, "open_topic", return_value={
                    "meta": {"tier": "P0", "participants": parts}}),
                patch.object(channels.topics_client, "post_message",
                             side_effect=_post_message),
                patch.object(channels.topics_client, "list_messages", return_value=[]),
                patch.object(channels.access_log, "touch", lambda *a, **k: None),
                patch.object(channels.activity_log, "append", lambda *a, **k: None),
                patch.object(channels, "_channel_message", AsyncMock()),
                patch.object(channels, "_start_turn", AsyncMock(return_value=True)),
                patch.object(channels, "_routing_plan", return_value=[]),
            ):
                stack.enter_context(cm)
            esito = await channels.post_channel_message(TIER, STANZA, testo, "owner")
        return esito, scritti

    def _righe_di_sistema(self, scritti) -> list[str]:
        return [t for autore, t, kind in scritti
                if autore == "system" and kind == "system"]

    async def test_an_absent_proxy_is_declared_in_the_room(self) -> None:
        """Il controllo rosso: oggi la stanza non riceve niente."""
        esito, scritti = await self._post(f"@{CHI} qual è lo stato?")
        righe = self._righe_di_sistema(scritti)
        self.assertEqual(1, len(righe), "nessuna riga nella stanza: il messaggio "
                                        "resta senza risposta e senza spiegazione")
        self.assertIn(CHI, righe[0])
        self.assertIn("non è connesso", righe[0])
        self.assertTrue(esito.get("proxy_assente"))

    async def test_it_says_the_mention_is_not_lost(self) -> None:
        """Senza questa metà la riga sembra un errore da rifare: la menzione
        resta nella storia, e il ponte la ritrova quando si ricollega."""
        _esito, scritti = await self._post(f"@{CHI} qual è lo stato?")
        self.assertIn("rientr", self._righe_di_sistema(scritti)[0].lower())

    async def test_a_connected_proxy_gets_no_line(self) -> None:
        """Se il ponte ascolta, risponde lui: una riga qui sarebbe rumore a ogni
        menzione servita."""
        presence.touch(CHI, TIER, STANZA)
        esito, scritti = await self._post(f"@{CHI} qual è lo stato?")
        self.assertEqual([], self._righe_di_sistema(scritti))
        self.assertFalse(esito.get("proxy_assente"))

    async def test_a_proxy_connected_to_another_room_is_not_listening_here(self) -> None:
        """Il token di un proxy è coniato per UNA stanza: `elsewhere` vuol dire
        che ascolta un'altra conversazione, non questa."""
        presence.touch(CHI, TIER, "altra")
        self.assertEqual("elsewhere", presence.stato(CHI, TIER, STANZA))
        _esito, scritti = await self._post(f"@{CHI} qual è lo stato?")
        self.assertEqual(1, len(self._righe_di_sistema(scritti)))

    async def test_a_bot_that_cannot_serve_the_tag_changes_nothing(self) -> None:
        """Il confine: un tag non servibile su un AGENTE è un altro problema
        (configurazione), e non deve iniziare a produrre righe da qui."""
        self.agents["worker"] = _a("worker", "normal", "P1", "2026-02-01T00:00:00Z")
        with patch.object(channels, "_provider_seal_ok", return_value=False):
            _esito, scritti = await self._post("@worker prendi tu")
        self.assertEqual([], self._righe_di_sistema(scritti))

    async def test_a_proxy_that_is_not_a_participant_gets_no_line(self) -> None:
        """`@qualcuno` che non è nella stanza è una menzione a un estraneo: la
        ragione esistente già lo dice, e non è l'assenza di un ponte."""
        _esito, scritti = await self._post(f"@{CHI} ci sei?",
                                          partecipanti=["owner", "worker"])
        self.assertEqual([], self._righe_di_sistema(scritti))

    async def test_the_reason_no_longer_calls_a_proxy_an_unroutable_ai(self) -> None:
        """La ragione tornava «non è un agente AI instradabile»: vera per il
        router, fuorviante per la stanza, dove un proxy È chi deve rispondere."""
        trace: dict = {}
        with patch.object(channels, "_provider_seal_ok", return_value=True):
            self.assertIsNone(channels._pick_responder(
                ["owner", "worker", CHI], "P0", CHI, f"@{CHI} stato?", trace=trace))
        self.assertEqual("tag-unserved", trace["mode"])
        self.assertIn("proxy", trace["reason"])
        self.assertNotIn("non è un agente AI instradabile", trace["reason"])


class IlPontePuoNonEssereNelRegistryTests(unittest.TestCase):
    """Guardia sul caso che il registry non conosce: senza tipo dichiarato non si
    inventa né una presenza né una riga (stessa regola di `_partecipanti_con_presenza`)."""

    def test_an_unknown_name_is_not_treated_as_a_proxy(self) -> None:
        with patch.object(channels.registry, "get_by_name", lambda _n: None):
            self.assertFalse(channels._proxy_partecipante("chiunque", ["chiunque"]))

    def test_a_bot_is_not_a_proxy(self) -> None:
        spec = SimpleNamespace(type="bot", name="worker")
        with patch.object(channels.registry, "get_by_name", lambda _n: spec):
            self.assertFalse(channels._proxy_partecipante("worker", ["worker"]))


if __name__ == "__main__":
    unittest.main()
