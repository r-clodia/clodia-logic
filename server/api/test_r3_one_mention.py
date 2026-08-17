"""router-notebook R3 riscritta: una menzione per messaggio, e la seconda si
chiede a CHI HA SCRITTO il messaggio.

    «Nuova regola 1 messaggio max 1 menzione. Un messaggio con due menzioni chiede
     conferma. Se l'ha generato un agente A che menziona sia B che C, allora viene
     chiesta conferma all'agente A con una menzione diretta (chi intendevi
     attivare? B o C)»
                                                        — Davide, 17 ago 2026

La versione precedente («due chiedono, tre rifiutano») sbagliava il DESTINATARIO
della domanda, non il conteggio:

· quando il messaggio ambiguo veniva da un agente, il dialogo con le pillole era
  rivolto agli umani. Nessuno aspettava una domanda: le pillole restavano lì, il
  turno non partiva, e il canale sembrava piantato invece che in attesa. L'unico
  che potesse rispondere subito — l'agente che aveva appena scritto i due nomi —
  era il solo a non essere interpellato;
· nel percorso degli agenti non si chiedeva affatto: `plan[:1]`, il primo tag
  vinceva in silenzio con una riga di log.

Due soglie con due esiti diversi (2 → dialogo, 3+ → rifiuto) erano il resto della
confusione: niente rende «quale fra B, C, D» più difficile di «quale fra B e C».
"""
from __future__ import annotations

import contextlib
import os
import unittest
from unittest.mock import AsyncMock, patch

from . import channels
from .test_channels import _a


def _post_sink() -> tuple[list, callable]:
    posts: list[dict] = []

    def post(_tier, _name, author, text, kind="human", **_kwargs):
        row = {"id": str(len(posts) + 1), "author": author, "text": text, "kind": kind}
        posts.append(row)
        return row

    return posts, post


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.agents = {
            "clodia": _a("clodia", "super", "P3", "2026-01-01T00:00:00Z"),
            "worker": _a("worker", "normal", "P1", "2026-02-01T00:00:00Z"),
            "accountant": _a("accountant", "normal", "P1", "2026-02-01T00:00:01Z"),
            "reviewer": _a("reviewer", "normal", "P1", "2026-02-01T00:00:02Z"),
            "owner": _a("owner", "human", role="superadmin"),
        }
        self._orig_get = channels.registry.get_by_name
        self._orig_track = channels._track_routing_decision
        channels.registry.get_by_name = lambda n: self.agents.get(n)
        channels._track_routing_decision = lambda _payload: None
        os.environ.pop("CHANNEL_MULTI_RESPONDER", None)

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig_get
        channels._track_routing_decision = self._orig_track

    def _channel(self, participants, history=None):
        """Le patch comuni: canale aperto, provider idoneo, log spenti.

        Ritorna `(posts, apri)`: `apri()` è un context manager che entra in tutte
        le patch insieme — `with a, *lista:` non è sintassi valida.
        """
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


class AHumanAsksWithChoicesTests(_Base):
    """Autore umano: il dialogo resta, ma senza `both` e senza soglia a tre."""

    async def _human(self, text, participants=None):
        start = AsyncMock(return_value=True)
        posts, apri = self._channel(
            participants or ["owner", "worker", "accountant", "reviewer"])
        with apri(patch.object(channels, "_start_turn", start)):
            result = await channels.post_channel_message("P0", "ops", text, "owner")
        return result, posts, start

    async def test_two_mentions_offer_the_two_names_and_nothing_else(self) -> None:
        """`both` è rimosso: era l'ultima via per cui un messaggio avviava due
        turni, e «max 1 menzione» la esclude. Due agenti sullo stesso argomento
        restano possibili — con due messaggi."""
        result, posts, start = await self._human("@worker @accountant guardate qui")

        self.assertTrue(result["routing_dialog"])
        self.assertEqual(["worker", "accountant"], result["choices"])
        self.assertIn("<!-- choices=worker,accountant -->", posts[-1]["text"])
        self.assertNotIn("both", posts[-1]["text"].lower())
        start.assert_not_awaited()

    async def test_three_mentions_ask_the_same_question_and_do_not_refuse(self) -> None:
        """Cade il rifiuto a 3+: il turno non viene più lasciato per strada."""
        result, posts, start = await self._human(
            "@worker @accountant @reviewer chi se ne occupa?")

        self.assertTrue(result["routing_dialog"])
        self.assertEqual(["worker", "accountant", "reviewer"], result["choices"])
        self.assertNotIn("routing_refused", result)
        self.assertIn("<!-- choices=worker,accountant,reviewer -->", posts[-1]["text"])
        start.assert_not_awaited()

    async def test_the_dialog_is_still_bound_to_its_author(self) -> None:
        """Il vincolo di R3 che NON cambia: risponde chi ha scritto il messaggio.
        Senza il marker un altro partecipante deciderebbe il turno di un altro."""
        _result, posts, _start = await self._human("@worker @accountant a voi")
        self.assertIn('<!-- routing-request={"owner":"owner","source":"1"}', posts[-1]["text"])

    async def test_one_mention_still_routes_directly(self) -> None:
        """La norma, e il caso che deve restare gratuito: nessun dialogo."""
        result, posts, start = await self._human("@worker pensaci tu")
        self.assertEqual(1, start.await_count)
        self.assertEqual("worker", start.await_args.args[3].name)
        self.assertNotIn("routing_dialog", result)
        self.assertEqual([], [p for p in posts if p["author"] == "router"])

    async def test_an_unserviceable_tag_does_not_make_a_message_ambiguous(self) -> None:
        """Si contano solo le menzioni che AVVIEREBBERO un turno. `@nessuno` non
        era un target nemmeno prima: contarlo trasformerebbe un errore di battitura
        in una domanda."""
        result, _posts, start = await self._human(
            "@worker @nessuno vedete voi", ["owner", "worker"])
        self.assertEqual(1, start.await_count)
        self.assertNotIn("routing_dialog", result)

    async def test_a_soft_tag_does_not_count(self) -> None:
        """`$nome` è una citazione e non apre un turno (R12): non può rendere
        ambiguo un messaggio con una sola menzione vera."""
        result, _posts, start = await self._human("@worker $accountant procedi")
        self.assertEqual(1, start.await_count)
        self.assertEqual("worker", start.await_args.args[3].name)
        self.assertNotIn("routing_dialog", result)


class AnAgentIsAskedBackTests(_Base):
    """Autore agente: la domanda torna a LUI, con una menzione diretta."""

    async def _delegate(self, from_agent, text, participants=None, history=None):
        start = AsyncMock(return_value=True)
        posts, apri = self._channel(
            participants or ["owner", "clodia", "worker", "accountant", "reviewer"],
            history)
        with apri(patch.object(channels, "_start_turn", start)):
            await channels._maybe_delegate("P0", "ops", from_agent, text, "owner", 0)
        return posts, start

    async def test_two_mentions_start_neither_and_ask_the_author(self) -> None:
        """Il cuore della richiesta. Prima partiva `worker` e `accountant` restava
        una riga di log; ora non parte nessuno dei due e il turno va all'autore."""
        posts, start = await self._delegate(
            "clodia", "Ci pensano @worker e @accountant")

        self.assertEqual(1, start.await_count)
        self.assertEqual("clodia", start.await_args.args[3].name,
                         "il turno deve tornare all'AUTORE, non al primo taggato")

    async def test_the_question_mentions_the_author_and_names_the_candidates(self) -> None:
        """«con una menzione diretta (chi intendevi attivare? B o C)»: senza il
        `@` all'autore il messaggio sarebbe un'altra pillola che nessuno raccoglie."""
        posts, _start = await self._delegate(
            "clodia", "Ci pensano @worker e @accountant")

        domanda = posts[-1]
        self.assertEqual("router", domanda["author"])
        self.assertIn("@clodia", domanda["text"])
        self.assertIn("worker", domanda["text"])
        self.assertIn("accountant", domanda["text"])

    async def test_the_turn_carries_the_disambiguation_directive(self) -> None:
        """L'agente va istruito su cosa fare, non solo interrogato: la direttiva
        chiede UNA sola menzione, che è anche ciò che impedisce il rimbalzo."""
        _posts, start = await self._delegate(
            "clodia", "Ci pensano @worker e @accountant")
        self.assertEqual("disambigua", start.await_args.args[6])

    async def test_three_mentions_from_an_agent_ask_the_same_way(self) -> None:
        posts, start = await self._delegate(
            "clodia", "@worker @accountant @reviewer uno di voi")
        self.assertEqual("clodia", start.await_args.args[3].name)
        for nome in ("worker", "accountant", "reviewer"):
            self.assertIn(nome, posts[-1]["text"])

    async def test_one_mention_from_an_agent_still_delegates(self) -> None:
        """Non-regressione: la delega normale non passa da nessuna domanda."""
        posts, start = await self._delegate("clodia", "Ci pensa @worker")
        self.assertEqual(1, start.await_count)
        self.assertEqual("worker", start.await_args.args[3].name)
        self.assertEqual([], [p for p in posts if p["author"] == "router"])

    async def test_a_self_mention_beside_another_agent_is_ambiguous(self) -> None:
        """A12: un'automenzione forka una nuova istanza. Quindi `@clodia @worker`
        scritto da clodia può voler dire *forkami* o *passa a worker* — ambiguo
        davvero, e la domanda va posta come per qualunque altra coppia."""
        self.agents["clodia"].multi_spawn = True
        posts, start = await self._delegate(
            "clodia#1", "Me ne occupo con @clodia e @worker")
        self.assertEqual("clodia", start.await_args.args[3].name)
        self.assertIn("@clodia", posts[-1]["text"])

    async def test_a_soft_tag_does_not_make_the_delegation_ambiguous(self) -> None:
        with patch.dict(os.environ, {"CHANNEL_SOFT_ACK_RATE": "0"}):
            posts, start = await self._delegate(
                "clodia", "Ci pensa @worker, $accountant per conoscenza")
        self.assertEqual("worker", start.await_args.args[3].name)
        self.assertEqual([], [p for p in posts if p["author"] == "router"])


class AskedOnceNeverTwiceTests(_Base):
    """Il rimbalzo è il modo in cui questo disegno potrebbe costare token in
    silenzio: A chiede a B, B risponde ambiguo, si richiede, e via."""

    async def test_a_second_ambiguous_reply_starts_nothing(self) -> None:
        start = AsyncMock(return_value=True)
        storia = [{"id": "9", "author": "router", "kind": "system",
                   "text": "@clodia hai menzionato @worker e @accountant: chi "
                           "intendevi attivare?\n\n"
                           '<!-- routing-ask={"to":"clodia"} -->'}]
        posts, apri = self._channel(
            ["owner", "clodia", "worker", "accountant"], storia)
        with apri(patch.object(channels, "_start_turn", start)):
            await channels._maybe_delegate(
                "P0", "ops", "clodia", "Direi @worker e @accountant insieme",
                "owner", 0)

        start.assert_not_awaited()

    async def test_and_it_says_so_instead_of_going_quiet(self) -> None:
        """Un turno fermo che si dichiara è recuperabile; il silenzio no — ed era
        il difetto della versione precedente su un altro percorso."""
        start = AsyncMock(return_value=True)
        storia = [{"id": "9", "author": "router", "kind": "system",
                   "text": "@clodia ... \n\n" '<!-- routing-ask={"to":"clodia"} -->'}]
        posts, apri = self._channel(
            ["owner", "clodia", "worker", "accountant"], storia)
        with apri(patch.object(channels, "_start_turn", start)):
            await channels._maybe_delegate(
                "P0", "ops", "clodia", "@worker e @accountant", "owner", 0)

        self.assertTrue(posts, "nessun messaggio: il turno si fermerebbe in silenzio")
        self.assertEqual("router", posts[-1]["author"])

    async def test_the_question_is_asked_again_after_a_clean_turn(self) -> None:
        """«una volta per catena», non una volta per sempre: se in mezzo c'è stato
        un turno normale, una nuova ambiguità è una catena nuova."""
        start = AsyncMock(return_value=True)
        storia = [
            {"id": "9", "author": "router", "kind": "system",
             "text": '@clodia ...\n\n<!-- routing-ask={"to":"clodia"} -->'},
            {"id": "10", "author": "clodia", "kind": "agent", "text": "Ci pensa @worker"},
            {"id": "11", "author": "worker", "kind": "agent", "text": "Fatto."},
        ]
        posts, apri = self._channel(
            ["owner", "clodia", "worker", "accountant"], storia)
        with apri(patch.object(channels, "_start_turn", start)):
            await channels._maybe_delegate(
                "P0", "ops", "clodia", "Ora @worker e @accountant", "owner", 0)

        self.assertEqual(1, start.await_count)
        self.assertEqual("clodia", start.await_args.args[3].name)


class TheChoiceIsHonouredTests(_Base):
    """La risposta al dialogo umano: un nome parte, `both` non esiste più."""

    async def _reply(self, testo, dialogo):
        start = AsyncMock(return_value=True)
        storia = [{"id": "1", "author": "owner", "kind": "human",
                   "text": "@worker @accountant a voi"},
                  {"id": "2", "author": "router", "kind": "system", "text": dialogo}]
        posts, apri = self._channel(
            ["owner", "worker", "accountant"], storia)
        with apri(patch.object(channels, "_start_turn", start)):
            result = await channels.post_channel_message("P0", "ops", testo, "owner")
        return result, start

    def _dialogo(self) -> str:
        return ("Routing: scegli @worker o @accountant.\n\n"
                "<!-- choices=worker,accountant -->\n"
                # Il marker porta l'ID del messaggio sorgente, non il testo:
                # `_latest_routing_request` risolve l'id sullo storico e verifica
                # che l'autore combaci, così un messaggio successivo non può
                # sostituire il turno di cui il dialogo parlava.
                '<!-- routing-request={"owner":"owner","source":"1"} -->')

    async def test_naming_one_agent_starts_exactly_that_one(self) -> None:
        result, start = await self._reply(
            "> router: Routing: scegli @worker o @accountant.\n\n@router worker",
            self._dialogo())
        self.assertEqual(["worker"], result["responders"])
        self.assertEqual(1, start.await_count)

    async def test_asking_for_both_starts_nobody(self) -> None:
        """`both` non è più un'opzione: non deve restare un accesso di servizio
        che riapre il fan-out con una parola."""
        result, start = await self._reply(
            "> router: Routing: scegli @worker o @accountant.\n\n@router both",
            self._dialogo())
        self.assertEqual([], result.get("responders") or [])
        start.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
