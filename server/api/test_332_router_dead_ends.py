"""clodia-logic#332 · i due vicoli ciechi della catena di disambiguazione.

Il sintomo raccolto nel canale `software-house` il 2026-09-04: il router chiede
«chi intendevi attivare?», l'agente risponde con UNA sola menzione — quella
giusta — e la risposta è «la catena di delega ha raggiunto il limite di 5
passaggi: nessun turno è partito». Una domanda posta e poi non onorata.

La causa non è il limite: è che la domanda **paga un salto**. `_maybe_delegate`
avviava il turno di chiarimento con `hop + 1`, e la risposta rientrava valutata
su quel salto in più, dove il controllo del limite è il primo che scatta.
Conseguenza: chiedere costava più che tirare a indovinare, ed era proprio la
mossa prudente a uccidere la catena.

Gli altri due controlli tengono in piedi le altre due promesse della issue: una
seconda risposta ambigua resta RISOLVIBILE (le menzioni fornite diventano pill,
un click e la catena riparte) invece di essere un fondo cieco, e l'esaurimento
del limite lascia un segnale che un osservatore può leggere — è ciò che permette
di chiedersi «un'istanza è già attiva su questo lavoro?» prima di aprirne una a
valle, il doppio assegnamento che la issue racconta.
"""
from __future__ import annotations

import contextlib
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from . import channels
from .test_r3_one_mention import _Base

_LIMITE = 5


class _Catena(_Base):
    """Come `_Base._channel`, con due differenze che servono qui.

    La storia è VIVA: i messaggi che il router posta finiscono nello storico che
    il giro dopo rilegge. Senza, il caso «seconda volta di fila» non si può
    montare, ed è metà della issue.

    E il limite della catena è fissato a 5: è il valore con cui il guasto è stato
    osservato, e un test che dipende dall'ambiente non dice niente.
    """

    def setUp(self) -> None:
        super().setUp()
        self._orig_hops = os.environ.get("CLODIA_MAX_DELEGATION_HOPS")
        os.environ["CLODIA_MAX_DELEGATION_HOPS"] = str(_LIMITE)

    def tearDown(self) -> None:
        if self._orig_hops is None:
            os.environ.pop("CLODIA_MAX_DELEGATION_HOPS", None)
        else:
            os.environ["CLODIA_MAX_DELEGATION_HOPS"] = self._orig_hops
        super().tearDown()

    def _canale(self, participants, history=None):
        storia: list[dict] = list(history or [])

        def post(_tier, _name, author, text, kind="human", **_kwargs):
            row = {"id": str(len(storia) + 1), "author": author,
                   "text": text, "kind": kind}
            storia.append(row)
            return row

        patches = [
            patch.object(channels, "_provider_seal_ok", return_value=True),
            patch.object(channels.topics_client, "open_topic", return_value={
                "meta": {"tier": "P0", "participants": participants}}),
            patch.object(channels.topics_client, "post_message", side_effect=post),
            patch.object(channels.topics_client, "list_messages",
                         side_effect=lambda *_a, **_k: list(storia)),
            patch.object(channels.access_log, "touch", lambda *a, **k: None),
            patch.object(channels.activity_log, "append", lambda *a, **k: None),
            patch.object(channels, "_channel_message", AsyncMock()),
        ]

        @contextlib.contextmanager
        def apri(*extra):
            with contextlib.ExitStack() as stack:
                for cm in list(extra) + patches:
                    stack.enter_context(cm)
                yield

        return storia, apri


class TheClarificationDoesNotSpendTheBudgetTests(_Catena):

    async def test_the_question_is_not_one_more_hop(self) -> None:
        """Il turno di chiarimento parte sullo STESSO salto: è la delega di prima
        detta meglio, non una nuova."""
        start = AsyncMock(return_value=True)
        _storia, apri = self._canale(
            ["owner", "clodia", "worker", "accountant"])
        with apri(patch.object(channels, "_start_turn", start)):
            await channels._maybe_delegate(
                "P0", "ops", "clodia", "Ci pensano @worker e @accountant",
                "owner", _LIMITE - 1)

        self.assertEqual("clodia", start.await_args.args[3].name)
        self.assertEqual("disambigua", start.await_args.args[6])
        self.assertEqual(_LIMITE - 1, start.await_args.kwargs["hop"])

    async def test_the_answer_to_the_question_still_starts_a_turn(self) -> None:
        """Il giro completo, che è il guasto osservato: si chiede all'ultimo salto
        utile e la risposta con una sola menzione DEVE avviare il delegato."""
        start = AsyncMock(return_value=True)
        storia, apri = self._canale(["owner", "clodia", "worker", "accountant"])
        with apri(patch.object(channels, "_start_turn", start)):
            await channels._maybe_delegate(
                "P0", "ops", "clodia", "Ci pensano @worker e @accountant",
                "owner", _LIMITE - 1)
            hop_domanda = start.await_args.kwargs["hop"]
            start.reset_mock()
            # La risposta dell'autore: una sola menzione, quella giusta.
            storia.append({"id": "99", "author": "clodia", "kind": "ai",
                           "text": "@worker"})
            await channels._maybe_delegate(
                "P0", "ops", "clodia", "@worker", "owner", hop_domanda)

        self.assertEqual(
            1, start.await_count,
            "la risposta alla domanda del router non ha avviato nessun turno: "
            "il chiarimento ha consumato il budget della catena")
        self.assertEqual("worker", start.await_args.args[3].name)

    async def test_a_chain_over_the_limit_is_still_stopped(self) -> None:
        """Il freno resta: `hop=hop` non è «nessun limite». Oltre il limite non
        parte niente, chiarimento o no."""
        start = AsyncMock(return_value=True)
        _storia, apri = self._canale(["owner", "clodia", "worker", "accountant"])
        with apri(patch.object(channels, "_start_turn", start)):
            await channels._maybe_delegate(
                "P0", "ops", "clodia", "Ci pensano @worker e @accountant",
                "owner", _LIMITE)

        start.assert_not_awaited()


class ASecondAmbiguousReplyStaysResolvableTests(_Catena):

    def _storia_con_domanda(self) -> list[dict]:
        return [{"id": "9", "author": channels._ROUTING_DIALOG_AUTHOR,
                 "kind": "system",
                 "text": "@clodia hai menzionato @worker o @accountant: chi "
                         "intendevi attivare?\n\n"
                         '<!-- routing-ask={"to":"clodia"} -->'}]

    async def _seconda_volta(self, testo: str):
        start = AsyncMock(return_value=True)
        storia, apri = self._canale(
            ["owner", "clodia", "worker", "accountant"],
            self._storia_con_domanda())
        with apri(patch.object(channels, "_start_turn", start)):
            await channels._maybe_delegate("P0", "ops", "clodia", testo, "owner", 0)
        return storia, start

    async def test_the_dead_end_offers_the_mentions_it_received(self) -> None:
        """Invece di ripetere la domanda o tacere: le menzioni che l'agente ha
        scritto diventano pill. Un click è un messaggio con UNA sola menzione,
        che il ramo dei tag serve già — e la catena riparte da un umano."""
        storia, start = await self._seconda_volta("Direi @worker e @accountant")

        start.assert_not_awaited()
        ultimo = storia[-1]
        self.assertEqual(channels._ROUTING_DIALOG_AUTHOR, ultimo["author"])
        self.assertIn("<!-- choices=@worker,@accountant -->", ultimo["text"])

    async def test_the_pills_carry_the_identity_the_author_used(self) -> None:
        """Con due istanze vive, `@worker` non dice quale: la pill deve riportare
        l'etichetta scritta dall'autore, o il click è ambiguo a sua volta."""
        self.agents["worker"].multi_spawn = True
        storia, _start = await self._seconda_volta("@worker-3 e @accountant")

        self.assertIn("<!-- choices=@worker-3,@accountant -->", storia[-1]["text"])


class TheExhaustedChainLeavesASignalTests(_Catena):

    async def test_the_limit_publishes_an_event_and_wakes_the_monitor(self) -> None:
        """Prima c'erano un messaggio in canale e una riga di log: niente che un
        osservatore possa leggere per sapere che una menzione è morta lì."""
        start = AsyncMock(return_value=True)
        publish = AsyncMock()
        watch = MagicMock()
        _storia, apri = self._canale(["owner", "clodia", "worker"])
        with apri(patch.object(channels, "_start_turn", start),
                  patch.object(channels.bus, "publish", publish),
                  patch.object(channels, "_watch_report", watch),
                  patch.object(channels, "_spawn_bg", lambda _coro: None)):
            await channels._maybe_delegate(
                "P0", "ops", "clodia", "@worker fallo tu", "owner", _LIMITE)

        eventi = [c.args[0] for c in publish.await_args_list]
        limite = [e for e in eventi
                  if (e.payload or {}).get("mode") == "delega-non-servita"]
        self.assertEqual(1, len(limite), f"nessun evento di limite fra {eventi}")
        payload = limite[0].payload
        self.assertEqual(["worker"], payload["negati"])
        self.assertEqual(_LIMITE, payload["hop"])
        self.assertEqual(_LIMITE, payload["limite"])
        self.assertEqual("clodia", payload["from_agent"])

        self.assertEqual("delegation_limit", watch.call_args.args[2],
                         "il monitor non è stato avvisato: la menzione morta "
                         "resta invisibile a chi controlla i doppi assegnamenti")

    async def test_a_reply_without_targets_says_nothing(self) -> None:
        """Non-regressione: senza menzioni da servire non c'è niente da segnalare,
        o il canale si riempie di avvisi su menzioni che non c'erano."""
        start = AsyncMock(return_value=True)
        publish = AsyncMock()
        storia, apri = self._canale(["owner", "clodia", "worker"])
        with apri(patch.object(channels, "_start_turn", start),
                  patch.object(channels.bus, "publish", publish)):
            await channels._maybe_delegate(
                "P0", "ops", "clodia", "Fatto, nessuno da chiamare.", "owner",
                _LIMITE)

        self.assertEqual([], storia)
        publish.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
