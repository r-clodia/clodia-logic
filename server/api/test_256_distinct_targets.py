"""R3 · l'ambiguità si decide sui TARGET RISOLTI, non sulla lista dei tag
(clodia-platform#256).

    @clodia hai menzionato @sysadmin, @fullstack-dev o @fullstack-dev: chi
    intendevi attivare? Rispondi con UNA sola menzione.

Due etichette identiche non sono una scelta. Il salto è uno solo e spiega tutti
i sintomi: si contava e si stampava la lista dei **tag scritti**, mai l'insieme
dei **target risolti** — `[worker-2, worker-3]` collassava a `[worker, worker]`
col `_seed_name` e nessuno ricontrollava.

Quattro difetti, misurati sul codice di prima:

1. nessun dedup dopo il collasso al seed → due pill identiche, e la stessa frase
   nel messaggio del limite catena («@worker o @worker sono stato taggato»);
2. la soglia contava i tag, non gli agenti distinti: un messaggio con un solo
   agente da attivare veniva trattenuto, il turno pagato e — se la risposta
   portava di nuovo due tag dello stesso seed — perso del tutto;
3. il collasso al seed distruggeva la distinzione espressa: la domanda non poteva
   dire *quale* istanza, e la risposta nemmeno;
4. **non descritto nell'issue e il più costoso**: rispondere a un dialogo con
   `choices=worker,worker` avviava **due turni** dello stesso agente
   (`responders: ['worker','worker']`). I dialoghi già in cronologia restano
   cliccabili dopo il deploy, quindi il dedup deve stare anche nel LETTORE della
   risposta, non solo in chi la formula.

Confine deciso e non collassato: `@worker` e `@worker-3` nello stesso messaggio
restano DUE target (allocazione vs. quello spawn). Chiedere è più caro di
indovinare, ma indovinare deciderebbe al posto dell'autore proprio dove ha
espresso una differenza.
"""
from __future__ import annotations

import contextlib
import os
import unittest
from unittest.mock import AsyncMock, patch

from . import channels
from .test_channels import _a
from .test_r3_one_mention import _post_sink


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.agents = {
            "clodia": _a("clodia", "super", "P3", "2026-01-01T00:00:00Z"),
            "worker": _a("worker", "normal", "P1", "2026-02-01T00:00:00Z"),
            "accountant": _a("accountant", "normal", "P1", "2026-02-01T00:00:01Z"),
            "owner": _a("owner", "human", role="superadmin"),
        }
        # `@worker-2` indirizza uno spawn solo se il seed è multi_spawn.
        self.agents["worker"].multi_spawn = True
        self._orig_get = channels.registry.get_by_name
        self._orig_track = channels._track_routing_decision
        channels.registry.get_by_name = lambda n: self.agents.get(n)
        channels._track_routing_decision = lambda _payload: None
        os.environ.pop("CHANNEL_MULTI_RESPONDER", None)

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig_get
        channels._track_routing_decision = self._orig_track

    def _channel(self, participants, history=None):
        posts, post = _post_sink()
        patches = [
            patch.object(channels, "_provider_seal_ok", return_value=True),
            patch.object(channels.topics_client, "open_topic", return_value={
                "meta": {"tier": "P0", "participants": participants}}),
            patch.object(channels.topics_client, "post_message", side_effect=post),
            patch.object(channels.topics_client, "list_messages",
                         return_value=list(history or [])),
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

        return posts, apri

    async def _human(self, text, participants=None, history=None):
        start = AsyncMock(return_value=True)
        posts, apri = self._channel(
            participants or ["owner", "clodia", "worker", "accountant"], history)
        with apri(patch.object(channels, "_start_turn", start)):
            result = await channels.post_channel_message("P0", "ops", text, "owner")
        return result, posts, start

    async def _delegate(self, from_agent, text, hop=0, participants=None):
        start = AsyncMock(return_value=True)
        posts, apri = self._channel(
            participants or ["owner", "clodia", "worker", "accountant"])
        with apri(patch.object(channels, "_start_turn", start)):
            await channels._maybe_delegate(
                "P0", "ops", from_agent, text, "owner", hop)
        return posts, start

    @staticmethod
    def _router_posts(posts):
        return [p for p in posts if p["author"] == "router"]


class ADisplayNeverRepeatsANameTests(_Base):
    """Il difetto 1 dove si vede: la funzione che STAMPA l'elenco."""

    def test_the_list_of_names_is_deduplicated(self) -> None:
        """Cintura da una riga nel punto che compone la frase: copre anche i
        chiamanti futuri che ricadranno nello stesso errore."""
        self.assertEqual("@worker", channels._elenco_or(["worker", "worker"]))
        self.assertEqual(
            "@worker o @accountant",
            channels._elenco_or(["worker", "accountant", "worker"]))


class OneAgentIsNotAChoiceTests(_Base):
    """Difetto 2: se c'è un solo agente da attivare, non si chiede."""

    async def test_two_tags_of_the_same_seed_start_one_turn(self) -> None:
        """`@worker` e `@worker#2`: la forma con `#N` non indirizza più nulla
        (`_split_target`), quindi entrambi i tag chiedono LO STESSO agente.
        Trattenere il turno per «scegli fra @worker e @worker» costava un
        messaggio di sistema e un giro di token per una domanda senza risposta."""
        result, posts, start = await self._human("@worker @worker#2 procedi")

        self.assertEqual(1, start.await_count)
        self.assertEqual("worker", start.await_args.args[3].name)
        self.assertNotIn("routing_dialog", result)
        self.assertEqual([], self._router_posts(posts))

    async def test_an_agent_tagging_the_same_seed_twice_delegates(self) -> None:
        """Stesso caso dal percorso agente: era quello osservato dal vivo
        («@fullstack-dev hai menzionato @clodia, @clodia o @clodia»)."""
        posts, start = await self._delegate(
            "clodia", "ci pensa @worker, cioè @worker#2")

        self.assertEqual(1, start.await_count)
        self.assertEqual("worker", start.await_args.args[3].name)
        self.assertEqual([], self._router_posts(posts))


class TheQuestionDistinguishesTheInstancesTests(_Base):
    """Difetti 1 e 3: due istanze vere sono due opzioni, e si vedono diverse."""

    async def test_the_choices_carry_the_identity_the_author_used(self) -> None:
        result, posts, start = await self._human(
            "@worker-2 @worker-3 chi di voi due?")

        self.assertTrue(result["routing_dialog"])
        self.assertEqual(["worker-2", "worker-3"], result["choices"])
        self.assertIn("<!-- choices=worker-2,worker-3 -->", posts[-1]["text"])
        start.assert_not_awaited()

    async def test_the_agent_question_names_both_instances(self) -> None:
        posts, start = await self._delegate(
            "clodia", "ci pensano @worker-2 e @worker-3")

        domanda = posts[-1]["text"]
        self.assertIn("@worker-2", domanda)
        self.assertIn("@worker-3", domanda)
        self.assertNotIn("@worker o @worker", domanda)
        # la domanda torna all'autore, che è l'unico a sapere cosa intendeva
        self.assertEqual("clodia", start.await_args.args[3].name)

    async def test_the_chain_limit_notice_says_each_name_once(self) -> None:
        """Il secondo esempio testuale dell'issue: «@fullstack-dev o
        @fullstack-dev sono stato taggato da fullstack-dev»."""
        posts, _start = await self._delegate(
            "clodia", "ci pensa @worker, cioè @worker#2", hop=99)

        avviso = posts[-1]["text"]
        self.assertIn("limite di", avviso)
        self.assertEqual(1, avviso.count("@worker"),
                         f"nome ripetuto nell'avviso: {avviso!r}")


class TheAnswerCanSelectAnInstanceTests(_Base):
    """Difetto 3, l'altra metà: una domanda a cui il sistema sa dare seguito."""

    def _storia(self, choices: str) -> list[dict]:
        return [
            {"id": "1", "author": "owner", "kind": "human",
             "text": "@worker-2 @worker-3 a voi"},
            {"id": "2", "author": "router", "kind": "system",
             "text": (f"Routing: scegli {channels._elenco_or(choices.split(','))}.\n\n"
                      f"<!-- choices={choices} -->\n"
                      '<!-- routing-request={"owner":"owner","source":"1"} -->')},
        ]

    async def test_choosing_a_spawn_starts_that_spawn(self) -> None:
        """Senza passare lo spawn a `_start_turn` la risposta «worker-3»
        ricadrebbe nell'allocazione normale: la domanda distinguerebbe le
        istanze e il sistema le confonderebbe di nuovo un passo dopo."""
        result, _posts, start = await self._human(
            "> router: Routing: scegli @worker-2 o @worker-3.\n\n@router worker-3",
            history=self._storia("worker-2,worker-3"))

        self.assertEqual(["worker"], result["responders"])
        self.assertEqual(1, start.await_count)
        self.assertEqual("worker-3", start.await_args.kwargs.get("spawn"))

    async def test_a_dialog_already_in_history_starts_one_turn_only(self) -> None:
        """Difetto 4. I messaggi con le choices duplicate sono già scritti e
        restano cliccabili dopo il deploy: il dedup deve stare anche qui, o il
        difetto più costoso sopravvive al fix che lo riguarda."""
        result, _posts, start = await self._human(
            "> router: Routing: scegli @worker o @worker.\n\n@router worker",
            history=self._storia("worker,worker"))

        self.assertEqual(["worker"], result["responders"])
        self.assertEqual(1, start.await_count,
                         "una pill cliccata una volta ha avviato due turni")


if __name__ == "__main__":
    unittest.main()
