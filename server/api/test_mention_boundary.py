"""Un indirizzo email non è una menzione (issue clodia-platform#255).

`_TAG_RE` non aveva confine sinistro, quindi il `@` agganciava tutto ciò che gli
stava davanti e il nome estratto era la prima etichetta del dominio:
`foo@bar.com` → `bar`. Il sintomo rumoroso — un agente convocato da un indirizzo
email, `clodia` collide con `*@clodia.*` — è il minore dei due.

Quello che ha tenuto in piedi il difetto per nove giorni è il sintomo silenzioso:
il tag fantasma non diventa un target (`bar` non risolve), ma finisce in
`hard_unserved`, e a quel punto `post_channel_message` crede che una convocazione
diretta sia stata tentata e sia fallita → **ritorna senza instradare per
rilevanza**. Cioè «scrivi a foo@bar.com per la fattura» veniva postato e non
riceveva risposta da nessuno. Davide l'aveva segnalato dall'altro lato il 10 ago
(«stranamente il messaggio non ha triggerato il router») e non era stato spiegato.

Il fix non aggiunge un confine alla seconda regex: fa convergere il router sul
parser del gateway, che era già corretto. Qui si misurano ENTRAMBI gli entry
point del router (`_tags`, `_tagged`) sulla stessa `GOLDEN_CASES` che la suite di
`clodia-tools` esegue sul suo.
"""
from __future__ import annotations

import contextlib
import inspect
import unittest
from unittest.mock import AsyncMock, patch

from . import channels, mentions
from .test_channels import _a


class GoldenCasesOnBothRouterEntryPointsTests(unittest.TestCase):
    """La tabella condivisa, eseguita sugli entry point di QUESTO repository."""

    def test_tags_matches_the_shared_rule_set(self) -> None:
        for testo, _men, hard, soft in mentions.GOLDEN_CASES:
            with self.subTest(testo=testo):
                self.assertEqual((hard, soft), channels._tags(testo))

    def test_tagged_returns_the_first_hard_tag_or_nothing(self) -> None:
        for testo, _men, hard, _soft in mentions.GOLDEN_CASES:
            with self.subTest(testo=testo):
                self.assertEqual(hard[0] if hard else None, channels._tagged(testo))

    def test_the_local_copy_of_the_parser_agrees_with_its_own_table(self) -> None:
        """Se questa copia divergesse da quella del gateway, il golden che
        viaggia dentro il modulo fa rosso da questo lato."""
        for testo, men, hard, soft in mentions.GOLDEN_CASES:
            with self.subTest(testo=testo):
                self.assertEqual(men, mentions.extract_mentions(testo))
                self.assertEqual((hard, soft), mentions.extract_tags(testo))


class TheAddressesMeasuredInTheIssueTests(unittest.TestCase):
    """I casi misurati nella issue, uno per uno: il nome estratto era il dominio."""

    def test_no_agent_is_hidden_in_an_email_address(self) -> None:
        for testo, atteso_prima in (
            ("scrivi a foo@bar.com", "bar"),
            ("manda a mario.rossi@cmm.it la LOI", "cmm"),
            ("la mia mail è davide@tomato.blue", "tomato"),
            ("ticket: support@github.com", "github"),
            ("costo 50@unita", "unita"),
        ):
            with self.subTest(testo=testo):
                self.assertNotEqual(atteso_prima, channels._tagged(testo))
                self.assertEqual(([], []), channels._tags(testo))

    def test_the_collision_that_summoned_a_real_agent(self) -> None:
        """Sintomo B: dei sedici agenti registrati, `clodia` collide — qualunque
        `*@clodia.` la convocava."""
        self.assertEqual(([], []), channels._tags("credenziali su a@clodia.io"))

    def test_pasting_a_command_no_longer_starts_a_turn(self) -> None:
        """Sintomo C: `_tags` scartava le righe citate e nient'altro."""
        self.assertEqual(([], []), channels._tags("```\ncurl -u a@clodia.io\n```"))
        self.assertEqual(([], []), channels._tags("usa `ssh a@clodia` per entrare"))

    def test_a_real_mention_after_an_address_still_summons(self) -> None:
        self.assertEqual((["clodia"], []),
                         channels._tags("scrivi a foo@bar.com, poi @clodia rivedi"))

    def test_the_two_numeric_forms_still_parse(self) -> None:
        self.assertEqual((["clodia#2"], []), channels._tags("@clodia#2 senti"))
        self.assertEqual((["clodia-124"], []), channels._tags("@clodia-124 senti"))


class OneParserOnlyTests(unittest.TestCase):
    """Il secondo parser non deve tornare: era la causa, non il sintomo.

    `_tags` e `_tagged` sono i due soli punti da cui il router legge i tag, e
    servono cinque chiamanti (catena di risposta dell'agente, `_humans_tagged`,
    `post_channel_message`, bootstrap). Una regex locale in più qui è un difetto
    che si ripresenta su tutti e cinque.
    """

    def test_the_regexes_without_a_left_boundary_are_gone(self) -> None:
        src = inspect.getsource(channels)
        self.assertNotIn("_TAG_RE", src)
        self.assertNotIn("_SOFT_TAG_RE", src)

    def test_the_router_reads_the_tags_from_the_shared_module(self) -> None:
        with patch.object(mentions, "extract_tags",
                          return_value=(["sentinella"], [])) as fake:
            self.assertEqual((["sentinella"], []), channels._tags("@qualcuno"))
        fake.assert_called_once()


class SymptomAIsTheRegressionThatMattersTests(unittest.IsolatedAsyncioTestCase):
    """Un messaggio con un indirizzo email e nessuna menzione va per rilevanza.

    È il controllo che va tenuto: la correzione del parser si vede qui come
    «qualcuno risponde», che era il difetto vissuto dall'utente.
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

    async def test_a_message_with_an_address_and_no_mention_is_routed(self) -> None:
        result, start = await self._post(
            "scrivi a foo@bar.com per la fattura",
            plan=[(self.agents["worker"], "scrivi a foo@bar.com per la fattura")])
        self.assertEqual(1, start.await_count)
        self.assertEqual("worker", start.await_args.args[3].name)

    async def test_it_is_not_reported_as_an_unservable_mention(self) -> None:
        """La firma del difetto: `responder: None` con la nota della menzione
        non servibile, al posto dell'instradamento."""
        result, _start = await self._post(
            "manda a mario.rossi@cmm.it la LOI",
            plan=[(self.agents["worker"], "manda la LOI")])
        self.assertNotIn("non può essere servita", str(result.get("note") or ""))

    async def test_an_address_inside_a_real_mention_message_still_dispatches(self) -> None:
        _result, start = await self._post("@worker scrivi a foo@bar.com per la fattura")
        self.assertEqual(1, start.await_count)
        self.assertEqual("worker", start.await_args.args[3].name)

    async def test_a_command_pasted_in_the_channel_does_not_summon_its_domain(self) -> None:
        _result, start = await self._post(
            "```\ncurl -u a@accountant.io https://x\n```",
            plan=[(self.agents["worker"], "curl")])
        self.assertEqual(1, start.await_count)
        self.assertEqual("worker", start.await_args.args[3].name)


if __name__ == "__main__":
    unittest.main()
