"""Il ragionamento si attacca alla BOLLA, e solo se una bolla c'è.

clodia-platform#484. Il deposito (`agents/reasoning_log.py`) e l'accumulo
(`sdk_runtime`) sono verificati altrove: qui si guarda la giuntura fra i due,
cioè l'unico punto che sa insieme il testo pensato e il messaggio comparso nel
canale.

Due decisioni dell'owner sono scritte qui come test, perché sono proprio il
genere di cosa che una rifattorizzazione distratta inverte:

  - **l'aggancio è l'ULTIMA bolla del turno.** Un turno può valere più
    messaggi (#243) ma il ragionamento è uno: appenderlo a tutti lo mostrerebbe
    ripetuto, appenderlo al primo lo metterebbe prima dei passi che racconta;
  - **niente bolle fantasma.** Un turno che non ha prodotto nessun messaggio e
    nessun annuncio di guasto non lascia niente: il limite è accettato, e
    inventare una bolla per appenderci il ragionamento sarebbe peggio del
    difetto che si sta curando.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from . import channels


class _Chat:
    """Sessione finta: consegna blocchi e lascia un ragionamento da consumare."""

    principal = ""

    def __init__(self, blocchi, pensiero="ho ragionato così", errore=None):
        self.blocchi = list(blocchi)
        self.pensiero = pensiero
        self.errore = errore

    async def send_user_message(self, _prompt: str) -> str:
        cb = getattr(self, "on_visible_block", None)
        if cb is not None:
            for b in self.blocchi:
                await cb(b)
        if self.errore:
            raise self.errore
        return "\n\n".join(self.blocchi)


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.messages: list[dict] = []
        self.registrati: list[dict] = []

        def post(_tier, _name, author, text, kind="human", **_kw):
            row = {"id": f"msg-{len(self.messages) + 1}", "author": author,
                   "text": text, "kind": kind, "ts": str(len(self.messages) + 1)}
            self.messages.append(row)
            return row

        def record(tier, name, **kw):
            self.registrati.append({"tier": tier, "name": name, **kw})

        async def noop_async(*_a, **_kw):
            return None

        self._patches = [
            patch.object(channels.topics_client, "post_message", post),
            patch.object(channels.topics_client, "list_messages",
                         lambda *_a, **_kw: list(self.messages)),
            patch.object(channels, "_maybe_delegate", noop_async),
            patch.object(channels, "_typing", noop_async),
            patch.object(channels, "_channel_message", noop_async),
            patch.object(channels, "_topic_title", lambda *_a, **_kw: None),
            patch.object(channels, "_spawn_bg", lambda _c: _c.close()),
            patch.object(channels.reasoning_log, "record", record),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    async def _run(self, chat, pensiero=None):
        """Esegue il turno con `consuma_pensiero` che restituisce `pensiero`."""
        valore = pensiero if pensiero is not None else (
            {"text": chat.pensiero, "truncated": False} if chat.pensiero else None)
        with patch.object(channels, "consuma_pensiero", return_value=valore):
            return await channels._run_and_post_response(
                "SEAL-1", "ops", "clodia", chat, "prompt")


class LAggancio(_Base):
    async def test_la_risposta_unica_porta_il_suo_ragionamento(self) -> None:
        await self._run(_Chat(["Ecco la risposta."]))
        self.assertEqual(1, len(self.registrati))
        voce = self.registrati[0]
        self.assertEqual("msg-1", voce["message_id"])
        self.assertEqual("ho ragionato così", voce["text"])
        self.assertEqual(("SEAL-1", "ops"), (voce["tier"], voce["name"]))

    async def test_con_piu_bolle_si_attacca_allultima(self) -> None:
        with patch.dict(os.environ, {"CLODIA_BUBBLE_PER_BLOCK": "1"}):
            await self._run(_Chat(["Guardo subito.", "Trovato.", "Fatto."]))
        self.assertEqual(["msg-3"], [v["message_id"] for v in self.registrati])

    async def test_lo_spawn_registrato_e_lautore_della_bolla(self) -> None:
        """Deve combaciare con l'autore che si legge sulla bolla, o in UI il
        ragionamento risulterebbe di un'altra istanza."""
        await self._run(_Chat(["Risposta."]))
        self.assertEqual(self.messages[-1]["author"], self.registrati[0]["spawn"])


class NienteBolleFantasma(_Base):
    async def test_un_turno_senza_ragionamento_non_scrive_niente(self) -> None:
        await self._run(_Chat(["Risposta."], pensiero=None))
        self.assertEqual([], self.registrati)

    async def test_un_ragionamento_senza_nessuna_bolla_si_perde(self) -> None:
        """IL LIMITE DICHIARATO. Il post nel canale fallisce: nessuna bolla è
        comparsa, e non se ne inventa una per appenderci il ragionamento.

        Vale anche come ordine delle operazioni: si conserva DOPO che il
        messaggio esiste, mai prima — una voce che punta a un id mai nato
        resterebbe nello store senza nessuna bolla da cui aprirla.
        """
        def esplode(*_a, **_kw):
            raise RuntimeError("gateway giù")

        with patch.object(channels.topics_client, "post_message", esplode):
            esito = await self._run(_Chat(["Risposta."]),
                                    pensiero={"text": "molto", "truncated": False})
        self.assertIsNone(esito)
        self.assertEqual([], self.registrati)

    async def test_un_errore_di_scrittura_non_rompe_il_turno(self) -> None:
        """Conservare il ragionamento è un di più: non deve poter far fallire
        il turno che lo ha prodotto."""
        with patch.object(channels.reasoning_log, "record",
                          side_effect=OSError("disco pieno")):
            esito = await self._run(_Chat(["Risposta."]))
        self.assertEqual("Risposta.", esito)


class ILimitiDelPerimetro(_Base):
    async def test_il_ragionamento_non_finisce_nellactivity_log(self) -> None:
        """L'activity log è indicizzato per AGENTE e non sa il tier: scriverci
        il ragionamento mescolerebbe canali di clearance diversa nello stesso
        file (è la correzione del commento che prometteva il contrario)."""
        with patch.object(channels.activity_log, "append") as act:
            await self._run(_Chat(["Risposta."]))
        for chiamata in act.call_args_list:
            self.assertNotIn("ho ragionato così", str(chiamata))


if __name__ == "__main__":
    unittest.main()
