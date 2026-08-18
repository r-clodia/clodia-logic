"""`$nome` è una citazione, non un'invocazione.

Sintomo: nel canale bilancio-tomato-2026 gli agenti usavano `@` quasi sempre (125
mention hard contro 16 soft su 207 messaggi). La diagnosi ovvia era «scelgono il
sigillo sbagliato», ma nel runtime i due sigilli facevano la STESSA cosa —
`$` avviava un turno come `@` — e la direttiva soft ordinava di postare un cenno
anche a chi non aveva nulla da aggiungere. Cioè: `$` costava come `@` più un
messaggio vuoto, e la distinzione che chiedevamo di usare non esisteva.
"""
from __future__ import annotations

import contextlib
import os
import unittest
from unittest.mock import AsyncMock, patch

from . import channels
from .test_channels import _a


class SoftAckSamplingTests(unittest.TestCase):
    """Campionamento deterministico, non un dado."""

    def test_the_same_message_always_decides_the_same_way(self):
        """Un retry non deve raddoppiare il cenno, e un replay del canale deve
        ricostruire la stessa storia."""
        args = ("commercialista", "avvocato", "riprendo il punto sul bilancio")
        first = channels._soft_ack_selected(*args)
        for _ in range(20):
            self.assertIs(channels._soft_ack_selected(*args), first)

    def test_a_rate_of_zero_never_acks(self):
        with patch.dict("os.environ", {"CHANNEL_SOFT_ACK_RATE": "0"}):
            self.assertFalse(channels._soft_ack_selected("a", "b", "x"))

    def test_a_rate_of_one_always_acks(self):
        with patch.dict("os.environ", {"CHANNEL_SOFT_ACK_RATE": "1"}):
            self.assertTrue(channels._soft_ack_selected("a", "b", "x"))

    def test_an_explicit_rate_still_samples_a_minority(self):
        """La manopola riaccesa deve ancora campionare, non aprire tutto. Non si
        asserisce 1/5 esatto: un test sull'esattezza di un hash misurerebbe
        l'hash, non il comportamento."""
        with patch.dict("os.environ", {"CHANNEL_SOFT_ACK_RATE": "0.2"}):
            hits = sum(channels._soft_ack_selected("agente", "collega", f"msg {i}")
                       for i in range(400))
        self.assertGreater(hits, 20)
        self.assertLess(hits, 160)

    def test_by_default_nothing_is_sampled(self):
        """R12 · `$nome` non avvia MAI un turno: la manopola resta, ma spenta.

        Con un default a 0.2 una citazione su cinque apriva un turno — poco, ma
        «mai» non ammette una frazione. La manopola non si rimuove: riaccenderla
        (il «segno di vita» del 17 ago) non deve costare un deploy.
        """
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(channels._soft_ack_rate(), 0.0)
            hits = sum(channels._soft_ack_selected("agente", "collega", f"msg {i}")
                       for i in range(200))
        self.assertEqual(hits, 0)

    def test_an_unparseable_rate_falls_back_instead_of_raising(self):
        with patch.dict("os.environ", {"CHANNEL_SOFT_ACK_RATE": "molto"}):
            self.assertEqual(channels._soft_ack_rate(), 0.0)


class SoftDirectiveTests(unittest.TestCase):
    def test_the_citation_directive_no_longer_mandates_a_nod(self):
        """Ordinare un cenno «anche se non hai nulla da aggiungere» fabbricava
        rumore per istruzione: era la riga che riempiva il canale."""
        d = channels._tag_directive("soft", "commercialista", "testo")
        self.assertIsNotNone(d)
        self.assertIn("UNA RIGA", d)
        self.assertIn("Nessun lavoro", d)
        self.assertNotIn("altrimenti posta", d)

    def test_the_direct_directive_states_the_cost_of_a_hard_mention(self):
        """La direttiva presentava `@` e `$` come un menu, senza criterio né
        costo. A quel punto `@` è la scelta razionale: è lo strumento più forte
        per «portare a casa l'obiettivo», che è ciò che le chiediamo."""
        d = channels._tag_directive("direct", "davide", "testo")
        self.assertIn("apre un turno completo", d)
        self.assertIn("non apre un turno", d)
        self.assertIn("In dubbio", d)

    def test_soft_ack_shares_the_citation_directive(self):
        self.assertEqual(channels._tag_directive("soft-ack", "x", "t"),
                         channels._tag_directive("soft", "x", "t"))


class TheHopSaysWhyTests(unittest.TestCase):
    """R12, punto aperto (a): un `@` da un agente porta una giustificazione.

    Non come campo obbligatorio — sarebbe un cambio di protocollo, con turni che
    si fermano per un metadato mancante. La frase che l'agente ha già scritto è
    la giustificazione: `_mention_context` la ritaglia e finisce nel `reason`
    della decisione di routing, insieme all'hop.
    """

    def test_the_sentence_around_the_mention_is_the_reason(self):
        self.assertEqual(
            channels._mention_context(
                "Ho finito il triage. @worker prendi la issue 189, è circoscritta. "
                "Poi vediamo.", "worker"),
            "@worker prendi la issue 189, è circoscritta.")

    def test_a_quoted_mention_is_not_this_message_summoning_anybody(self):
        """Le righe citate sono escluse anche da `_tags`: se il `@` vive solo là,
        non c'è nessuna convocazione di cui dare il motivo."""
        self.assertEqual(
            channels._mention_context("> clodia: @worker pensaci tu\nRicevuto.",
                                      "worker"),
            "")

    def test_a_missing_mention_returns_nothing_instead_of_guessing(self):
        self.assertEqual(channels._mention_context("nessun tag qui", "worker"), "")

    def test_a_wall_of_text_is_cut_and_says_so(self):
        """Il `reason` finisce in una barra e in un log: non ci si versa dentro un
        messaggio intero."""
        motivo = channels._mention_context("@worker " + "x" * 500, "worker")
        self.assertLessEqual(len(motivo), 160)
        self.assertTrue(motivo.endswith("…"))


class AHumanCitationDoesNotActivateTests(unittest.IsolatedAsyncioTestCase):
    """R12 sul percorso UMANO: `$nome` scritto da una persona non apre un turno.

    Il difetto stava tutto in una riga di `post_channel_message`: i tag soft
    finivano in `targets` come i `@`, con kind "soft", e da lì in `_start_turn`.
    Restava invisibile perché con `CHANNEL_MULTI_RESPONDER` a OFF un `@` accanto
    tagliava la citazione a `targets[:1]` — quindi il caso misto sembrava
    corretto mentre la citazione da sola attivava, e col flag ON attivava
    comunque. Un difetto che si vede solo in una configurazione è un difetto che
    resta.
    """

    def setUp(self) -> None:
        self.agents = {
            "worker": _a("worker", "normal", "P1", "2026-02-01T00:00:00Z"),
            "accountant": _a("accountant", "normal", "P1", "2026-02-01T00:00:01Z"),
            "owner": _a("owner", "human", role="superadmin"),
        }
        self._orig_get = channels.registry.get_by_name
        self._orig_track = channels._track_routing_decision
        channels.registry.get_by_name = lambda n: self.agents.get(n)
        channels._track_routing_decision = lambda _payload: None

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig_get
        channels._track_routing_decision = self._orig_track

    @contextlib.contextmanager
    def _channel(self, start, plan=None):
        """Canale aperto, provider idoneo, log spenti — e il routing per
        rilevanza reso deterministico: qui si misura chi parte per via del
        sigillo, non la pertinenza semantica."""
        with contextlib.ExitStack() as stack:
            for cm in (
                patch.object(channels, "_provider_seal_ok", return_value=True),
                patch.object(channels.topics_client, "open_topic", return_value={
                    "meta": {"tier": "P0",
                             "participants": ["owner", "worker", "accountant"]}}),
                patch.object(channels.topics_client, "post_message",
                             side_effect=lambda *_a, **_k: {"id": "1"}),
                patch.object(channels.topics_client, "list_messages",
                             return_value=[]),
                patch.object(channels.access_log, "touch", lambda *a, **k: None),
                patch.object(channels.activity_log, "append", lambda *a, **k: None),
                patch.object(channels, "_channel_message", AsyncMock()),
                patch.object(channels, "_start_turn", start),
                patch.object(channels, "_routing_plan", return_value=list(plan or [])),
            ):
                stack.enter_context(cm)
            yield

    async def _post(self, text, plan=None):
        start = AsyncMock(return_value=True)
        with self._channel(start, plan):
            result = await channels.post_channel_message("P0", "ops", text, "owner")
        return result, start

    async def test_a_citation_alone_starts_nobody(self) -> None:
        """Il caso che oggi attiva: `$accountant` e nient'altro."""
        _result, start = await self._post("$accountant per conoscenza, ho finito")
        start.assert_not_awaited()

    async def test_a_citation_does_not_become_a_turn_with_fan_out_on(self) -> None:
        """Col flag ON la citazione partiva accanto al `@`: non è il taglio a
        `targets[:1]` che deve tenere la regola."""
        with patch.dict(os.environ, {"CHANNEL_MULTI_RESPONDER": "1"}):
            _result, start = await self._post("@worker procedi, $accountant guarda")
        self.assertEqual(1, start.await_count)
        self.assertEqual("worker", start.await_args.args[3].name)

    async def test_a_citation_leaves_the_message_to_the_normal_routing(self) -> None:
        """Non attivare il citato non vuol dire ammutolire il canale: un
        messaggio con sole citazioni è un messaggio senza convocazioni, e segue
        la strada di quelli senza tag — il routing per rilevanza."""
        _result, start = await self._post(
            "$accountant una nota sul bilancio", plan=[(self.agents["worker"], "testo")])
        self.assertEqual(1, start.await_count)
        self.assertEqual("worker", start.await_args.args[3].name)

    async def test_the_citation_is_not_reported_as_a_responder(self) -> None:
        _result, start = await self._post("$accountant e $worker, per conoscenza")
        start.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
