"""Il relay serve un trasporto che NON è Telegram (clodia-platform#521).

Il criterio della issue, e la ragione per cui questo file esiste: finché le
prove del relay passano tutte da Telegram, «neutro rispetto al trasporto» è una
parola nel docstring. Qui c'è un adapter finto — un trasporto che non esiste,
con un suo schema di URI e una sua forma di identità — e il nucleo deve
riconoscere il mittente vagliato, rifiutare quello che non lo è, trattare la
chat in whitelist e attivarsi su una conversazione 1:1, senza una riga di codice
dedicata.

Vale anche come esempio eseguibile per chi scriverà il prossimo adapter: ciò
che serve implementare è esattamente quello che sta in `_Fake`.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import channel_relay, relay_adapters
from .relay_adapters import RelayAdapter


class _Fake(RelayAdapter):
    """Un trasporto immaginario: identità `fx:<utente>`, chat `fx:<id>`."""

    connector = "fakephone"
    transport_label = "fakephone"
    uri_scheme = "fx"

    def __init__(self, updates=None):
        self.inviati: list = []
        self.thread_inviati: list = []
        self.updates = updates or []
        self.pollato = 0

    async def poll(self, timeout: int) -> list:
        self.pollato += 1
        return self.updates

    async def send(self, chat_id, text: str, thread_id=None) -> None:
        self.inviati.append((str(chat_id), text))
        self.thread_inviati.append(thread_id)

    async def download(self, file_ref: dict) -> str | None:
        return "eA=="

    def sender_uri(self, msg: dict) -> str | None:
        who = str((msg or {}).get("from_username") or "").strip()
        return f"fx:{who}" if who else None

    def chat_uri(self, chat_id) -> str | None:
        return f"fx:{chat_id}" if str(chat_id or "").strip() else None


class _Base(unittest.IsolatedAsyncioTestCase):
    CHAT = "fx-77"
    BINDING = {"connector": "fakephone", "instance": "messaggero-1",
               "tier": "SEAL-1", "topic": "ops"}

    def setUp(self):
        self.stato = Path(tempfile.mkdtemp(prefix="relay-fake-"))
        self.addCleanup(shutil.rmtree, self.stato, ignore_errors=True)
        self.postati: list = []
        self.turni: list = []
        self.chiesto: list = []
        self.adapter = _Fake()
        relay_adapters.register(self.adapter)
        self.addCleanup(relay_adapters.forget, self.adapter.connector)

    def _ambiente(self, vetted_uris):
        """Il gateway risponde `vetted` solo per gli URI elencati: è lui a
        decidere, e il relay deve limitarsi a chiedere nella forma giusta."""
        def _gw(path, params=None):
            self.chiesto.append(params)
            return type("R", (), {"raise_for_status": lambda self: None,
                                  "json": lambda self: {
                                      "vetted": params["uri"] in vetted_uris}})()

        async def _open(tier, name):
            return {"meta": {"participants": ["clodia", "messaggero-1"]}}

        async def _post(tier, name, autore, testo, kind=None):
            self.postati.append(testo)

        async def _turn(tier, name, meta, trigger_text="", **kw):
            self.turni.append(trigger_text)
            self.turn_kw = kw

        async def _put(tier, name, fname, b64):
            self.salvato = (fname, b64)

        return [
            patch("server.api.observe._gw", _gw),
            patch.object(channel_relay, "data_path", lambda _n: self.stato),
            patch.object(channel_relay.topics_client, "async_open_topic", _open),
            patch.object(channel_relay.topics_client, "async_post_message", _post),
            patch.object(channel_relay.topics_client, "async_put_file", _put),
            patch.object(channel_relay, "run_topic_turn", _turn),
            # The seed registry is empty under test: clodia answers (#531).
            patch.object(channel_relay, "_can_deliver", lambda a, s=None: True),
            patch("server.api.channels._pick_responder",
                  lambda *a, **k: type("S", (), {"name": "clodia"})()),
        ]

    async def _relay(self, messaggi, vetted_uris=()):
        patches = self._ambiente(set(vetted_uris))
        for p in patches:
            p.start()
        try:
            await channel_relay._relay_chat(self.CHAT, self.BINDING, messaggi)
        finally:
            for p in reversed(patches):
                p.stop()


class MittenteVagliatoTests(_Base):
    MSG = {"message_id": 1, "from_username": "ada", "from": "ada",
           "text": "@clodia riassumi"}

    async def test_la_domanda_al_gateway_usa_l_uri_del_trasporto(self):
        """Il nucleo non sa cosa sia `fx:`: lo chiede all'adapter e lo passa al
        gateway così com'è, con direzione e scope del BINDING."""
        await self._relay([dict(self.MSG)], vetted_uris={"fx:ada"})
        self.assertIn({"uri": "fx:ada", "direction": "ingress",
                       "scope": "SEAL-1/ops"}, self.chiesto)

    async def test_un_mittente_vagliato_innesca_il_turno(self):
        await self._relay([dict(self.MSG)], vetted_uris={"fx:ada"})
        self.assertTrue(any("Ricevuto" in t for _c, t in self.adapter.inviati))
        atteso = ("@clodia, riporto dal gruppo fakephone «fx-77» (chat_id fx-77) "
                  "il seguente messaggio di utente fakephone ada: '@clodia riassumi'")
        self.assertEqual(self.postati, [atteso])
        self.assertEqual(self.turni, [atteso])
        self.assertEqual(self.turn_kw["responder_hint"], "clodia")
        self.assertEqual(self.turn_kw["trigger_kind"], "external")

    async def test_un_mittente_non_vagliato_e_rifiutato_e_il_topic_non_si_tocca(self):
        await self._relay([dict(self.MSG)], vetted_uris=set())
        self.assertEqual([t for _c, t in self.adapter.inviati], [channel_relay._DENY])
        self.assertEqual((self.postati, self.turni), ([], []))

    async def test_un_mittente_che_il_trasporto_non_sa_nominare_e_rifiutato(self):
        """`sender_uri` → None: non vagliabile per costruzione. Sul mittente non
        si chiede niente al gateway, che non saprebbe cosa gli si sta chiedendo
        (l'unica domanda resta quella sulla chat, che precede sempre)."""
        await self._relay([{"message_id": 2, "text": "@clodia ciao"}],
                          vetted_uris={"fx:ada"})
        self.assertEqual([t for _c, t in self.adapter.inviati], [channel_relay._DENY])
        self.assertEqual(self.chiesto, [{"uri": "fx:fx-77", "direction": "ingress",
                                         "scope": "SEAL-1/ops"}])

    async def test_la_chiacchiera_che_non_interpella_il_bot_non_arriva(self):
        await self._relay([{"message_id": 3, "from_username": "ada",
                            "text": "guardate il doc"}], vetted_uris={"fx:ada"})
        self.assertEqual((self.adapter.inviati, self.postati, self.turni), ([], [], []))


class ChatInWhitelistTests(_Base):
    async def test_chiunque_scriva_nella_chat_vagliata_non_viene_rifiutato(self):
        """La fonte vagliata è la CHAT (`fx:fx-77`): niente rifiuto, e nel canale
        entra solo la richiesta."""
        msgs = [{"message_id": 1, "from_username": "ignoto", "text": "ciao a tutti"},
                {"message_id": 2, "from_username": "ignoto", "text": "@clodia riassumi"}]
        await self._relay(msgs, vetted_uris={"fx:fx-77"})
        inviati = [t for _c, t in self.adapter.inviati]
        self.assertNotIn(channel_relay._DENY, inviati)
        self.assertTrue(any("Ricevuto" in t for t in inviati))
        self.assertEqual(len(self.postati), 1)
        self.assertNotIn("ciao a tutti", self.postati[0])
        self.assertIn("riassumi", self.postati[0])

    async def test_senza_richiesta_il_messaggero_resta_silente(self):
        await self._relay([{"message_id": 1, "from_username": "x", "text": "ciao"}],
                          vetted_uris={"fx:fx-77"})
        self.assertEqual((self.adapter.inviati, self.postati, self.turni), ([], [], []))


class UnoAUnoTests(_Base):
    async def test_una_reply_al_bot_vale_come_menzione(self):
        """L'attivazione 1:1: nessuna menzione nel testo, ma il messaggio è una
        risposta al bot — la regola è del nucleo, non del trasporto."""
        await self._relay([{"message_id": 1, "from_username": "ada", "from": "ada",
                            "text": "sì, procedi", "reply_to_bot": True}],
                          vetted_uris={"fx:ada"})
        self.assertEqual(len(self.turni), 1)
        self.assertTrue(self.turni[0].endswith("'sì, procedi'"))

    async def test_l_allegato_passa_dal_download_dell_adapter(self):
        await self._relay([{"message_id": 1, "from_username": "ada", "text": "@clodia leggi",
                            "file": {"file_id": "F1", "file_name": "offerta.pdf"}}],
                          vetted_uris={"fx:ada"})
        self.assertEqual(self.salvato, ("offerta.pdf", "eA=="))
        self.assertIn("salvato in `files/offerta.pdf`", self.postati[0])


class RegistroTests(unittest.IsolatedAsyncioTestCase):
    async def test_un_binding_su_un_connettore_sconosciuto_non_ferma_gli_altri(self):
        """Un pack disinstallato lascia dietro di sé binding orfani: devono
        essere ignorati, non far morire il ciclo di chi funziona ancora."""
        fake = _Fake(updates=[{"chat_id": "fx-77", "text": "@clodia ciao"}])
        relay_adapters.register(fake)
        self.addCleanup(relay_adapters.forget, fake.connector)
        bindings = {"trasporto-che-non-esiste": {"1": {"tier": "SEAL-1", "topic": "x"}},
                    "fakephone": {"fx-77": {"connector": "fakephone", "tier": "SEAL-1",
                                            "topic": "ops"}}}
        with patch.object(channel_relay.cb, "load", return_value=bindings), \
             patch.object(channel_relay, "_relay_chat") as relay:
            serviti = await channel_relay.run_poll_cycle(1)
        self.assertEqual(serviti, 1)
        relay.assert_called_once()
        self.assertEqual(fake.pollato, 1)

    async def test_lo_stato_e_per_connettore_e_chat(self):
        """Due trasporti possono usare lo stesso id di chat: se la chiave dello
        stato fosse la sola chat, i messaggi del secondo sarebbero scartati come
        «già visti»."""
        self.assertNotEqual(channel_relay._state_key("telegram", "42"),
                            channel_relay._state_key("fakephone", "42"))


if __name__ == "__main__":
    unittest.main()


class ThreadDelForumTests(_Base):
    """clodia-platform#548: in un gruppo-forum la risposta va nel thread da cui
    è arrivata la richiesta, non nel «Generale»."""
    MSG = {"message_id": 5, "from_username": "ada", "from": "ada",
           "text": "@clodia scrivi in funding", "thread_id": 42}

    async def test_la_conferma_va_nel_thread_della_richiesta(self):
        await self._relay([dict(self.MSG)], vetted_uris={"fx:ada"})
        self.assertTrue(self.adapter.inviati)
        self.assertEqual(set(self.adapter.thread_inviati), {42})

    async def test_il_rifiuto_va_nel_thread_della_richiesta(self):
        await self._relay([dict(self.MSG)], vetted_uris=set())
        self.assertEqual(self.adapter.thread_inviati, [42])

    async def test_la_chat_vagliata_conferma_nel_thread(self):
        await self._relay([dict(self.MSG)], vetted_uris={f"fx:{self.CHAT}"})
        self.assertEqual(self.adapter.thread_inviati, [42])

    async def test_il_canale_riceve_il_thread_da_passare_a_telegram_send(self):
        await self._relay([dict(self.MSG)], vetted_uris={"fx:ada"})
        self.assertIn("(chat_id fx-77, thread_id 42)", self.postati[0])

    async def test_senza_thread_tutto_resta_come_prima(self):
        msg = {k: v for k, v in self.MSG.items() if k != "thread_id"}
        await self._relay([msg], vetted_uris={"fx:ada"})
        self.assertEqual(set(self.adapter.thread_inviati), {None})
        self.assertIn("(chat_id fx-77) ", self.postati[0])
