"""Un messaggio di un proxy non è un messaggio di una persona (issue clodia-platform#221).

Misurato il 16 ago: un post di `clodia-primal` (un `type: proxy`) si persiste con
`kind: human`, perché l'etichetta la sceglie il gateway in base al fatto che la
chiamata sia on-behalf — e un token di proxy LO È. Dentro `clodia-logic` tre
regole leggono quell'etichetta e credono di parlare con una persona:

- chi possiede un dialogo di routing (`_latest_routing_request`);
- chi consuma il team bootstrap dell'owner (`_pending_team_bootstrap`);
- quale messaggio diventa l'esemplare supervisionato del router
  (`_latest_human_routing_context`).

La label autoritativa e il taint stanno nel gateway (sub-issue sua). Qui il repo
smette di credere all'etichetta e ricostruisce il fatto dall'AUTORE, che è
verità che abbiamo in casa: il registry sa chi è una persona.

Il predicato è FAIL-CLOSED su richiesta del security-engineer: umano solo se
l'autore risolve a un principal umano REGISTRATO. Autore ignoto, non
risolvibile o assente → NON umano. Il fail-open sarebbe la vulnerabilità vera:
è con il default «se non so, è umano» che un terzo possiede un dialogo di
routing o brucia il bootstrap dell'owner.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from ..agents.models import AgentSpec
from . import channels as ch
from . import router_config


def _a(name: str, type: str = "bot") -> AgentSpec:
    return AgentSpec.model_validate({
        "name": name, "description": "d", "display_name": name, "type": type,
        **({"model": "m", "system_prompt": "s.md"} if type not in {"human", "proxy"} else {}),
    })


AGENTS = {
    "davide": _a("davide", "human"),
    "clodia": _a("clodia"),
    "clodia-primal": _a("clodia-primal", "proxy"),
}


def _msg(author: str, text: str = "testo", kind: str = "human", mid: str = "1") -> dict:
    return {"id": mid, "author": author, "kind": kind, "text": text}


class _WithRegistry(unittest.TestCase):
    def setUp(self) -> None:
        p = patch.object(ch.registry, "get_by_name", side_effect=AGENTS.get)
        p.start()
        self.addCleanup(p.stop)


class FromHumanPredicateTests(_WithRegistry):

    def test_a_registered_person_is_human(self) -> None:
        self.assertTrue(ch._from_human(_msg("davide")))

    def test_a_proxy_is_not_human_even_when_labelled_human(self) -> None:
        """Il caso misurato: `author: clodia-primal`, `kind: human`."""
        self.assertFalse(ch._from_human(_msg("clodia-primal")))

    def test_an_unresolvable_author_is_not_human(self) -> None:
        """FAIL-CLOSED. Un autore che il registry non conosce non è una persona
        per default: è il default opposto che apre la porta."""
        self.assertFalse(ch._from_human(_msg("chi-sono-io")))
        self.assertFalse(ch._from_human(_msg("")))
        self.assertFalse(ch._from_human({"kind": "human"}))

    def test_a_bot_is_not_human(self) -> None:
        self.assertFalse(ch._from_human(_msg("clodia")))
        self.assertFalse(ch._from_human(_msg("clodia", kind="ai")))

    def test_a_person_writing_something_else_is_not_a_human_message(self) -> None:
        """Il predicato guarda ENTRAMBE le cose: l'etichetta e l'autore."""
        self.assertFalse(ch._from_human(_msg("davide", kind="system")))


class RoutingDialogOwnerTests(_WithRegistry):
    """`channels.py:171` — chi possiede il dialogo di routing (ramo legacy).

    Possedere il dialogo significa poter SCEGLIERE chi risponde: se un sistema
    terzo può esserne l'owner, sceglie lui.
    """

    def _dialog(self) -> dict:
        return {"id": "9", "author": ch._ROUTING_DIALOG_AUTHOR, "kind": "ai",
                "text": "chi intendevi? <!-- routing-choices=clodia,worker -->"}

    def test_a_proxy_does_not_own_the_dialog(self) -> None:
        msgs = [_msg("davide", "domanda umana", mid="1"),
                _msg("clodia-primal", "domanda di un sistema terzo", mid="2"),
                self._dialog()]
        got = ch._latest_routing_request(msgs)
        self.assertIsNotNone(got)
        self.assertEqual(got["owner"], "davide")

    def test_an_unknown_author_does_not_own_the_dialog(self) -> None:
        msgs = [_msg("chi-sono-io", "testo", mid="2"), self._dialog()]
        self.assertIsNone(ch._latest_routing_request(msgs))


class TeamBootstrapTests(_WithRegistry):
    """`channels.py:1401` — il bootstrap è a colpo singolo e si consuma al primo
    messaggio umano. Un post di un terzo non deve poterlo bruciare."""

    MARKER = "benvenuto <!-- team-bootstrap=clodia -->"

    def setUp(self) -> None:
        super().setUp()
        p = patch.object(ch, "_provider_seal_ok", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_a_proxy_post_does_not_consume_the_bootstrap(self) -> None:
        msgs = [{"id": "0", "author": "clodia", "kind": "ai", "text": self.MARKER},
                _msg("clodia-primal", "ciao a tutti", mid="1")]
        spec = ch._pending_team_bootstrap(msgs, ["clodia"], "SEAL-1")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.name, "clodia")

    def test_an_unknown_author_does_not_consume_it_either(self) -> None:
        msgs = [{"id": "0", "author": "clodia", "kind": "ai", "text": self.MARKER},
                _msg("chi-sono-io", "ciao", mid="1")]
        self.assertIsNotNone(ch._pending_team_bootstrap(msgs, ["clodia"], "SEAL-1"))

    def test_the_owner_still_consumes_it(self) -> None:
        msgs = [{"id": "0", "author": "clodia", "kind": "ai", "text": self.MARKER},
                _msg("davide", "ciao", mid="1")]
        self.assertIsNone(ch._pending_team_bootstrap(msgs, ["clodia"], "SEAL-1"))


class RoutingExemplarTests(_WithRegistry):
    """`channels.py:1815` — la finestra che diventa l'esemplare supervisionato
    del router finisce all'ultimo messaggio umano. Il testo di un sistema terzo
    non deve entrare nel corpus."""

    def _ctx(self, msgs: list[dict]) -> str:
        return ch._latest_human_routing_context(msgs, router_config.load())

    def test_the_window_stops_at_the_last_real_person(self) -> None:
        msgs = [_msg("davide", "la domanda vera", mid="1"),
                _msg("clodia-primal", "testo di un sistema terzo", mid="2")]
        ctx = self._ctx(msgs)
        self.assertIn("la domanda vera", ctx)
        self.assertNotIn("testo di un sistema terzo", ctx)

    def test_no_person_no_exemplar(self) -> None:
        self.assertEqual(self._ctx([_msg("clodia-primal", "solo terzi", mid="1")]), "")


class InboundKindTests(_WithRegistry):
    """Il `kind` che questo repo assegna a un innesco, ricostruito dall'autore.

    Tre esiti e non due: un agente della colonia non è una persona, ma non è
    nemmeno un sistema terzo — appiattirlo su `external` renderebbe l'avviso di
    provenienza rumore di fondo, e un avviso che compare sempre non si legge.
    """

    def test_a_person_triggers_a_human_kind(self) -> None:
        self.assertEqual(ch._inbound_kind("davide"), "human")

    def test_a_registered_agent_triggers_an_ai_kind(self) -> None:
        self.assertEqual(ch._inbound_kind("clodia"), "ai")

    def test_a_proxy_triggers_an_external_kind(self) -> None:
        self.assertEqual(ch._inbound_kind("clodia-primal"), "external")

    def test_an_unknown_trigger_is_external(self) -> None:
        self.assertEqual(ch._inbound_kind("chi-sono-io"), "external")
        self.assertEqual(ch._inbound_kind(None), "external")


class TriggerInternalTests(unittest.IsolatedAsyncioTestCase):
    """`channels.py:3681` — la porta da cui un sistema terzo fa partire lavoro.

    Oggi risponde `{"triggered": true}` e perde per strada CHI ha innescato: il
    contesto di routing riceve `author: "channel", kind: "human"`, quindi il
    turno non sa né chi lo ha svegliato né che il contenuto viene da fuori.
    """

    async def _trigger(self, by: str) -> tuple[dict, dict]:
        meta = {"owner": "davide", "participants": [by, "clodia"], "tier": "SEAL-1"}
        visto: dict = {}

        def _fake_turn(tier, name, meta, **kw):
            # registra al momento della CHIAMATA: `_spawn_bg` è finto e chiude
            # la coroutine senza eseguirla, quindi un corpo `async` non girerebbe
            visto.update(kw)

            async def _noop():
                return ("clodia", "ok")
            return _noop()

        async def _body():
            return {"text": "fai una cosa", "by": by}

        req = type("R", (), {})()
        req.json = _body

        with patch.object(ch.registry, "get_by_name", side_effect=AGENTS.get), \
             patch.object(ch.topics_client, "open_topic", return_value={"meta": meta}), \
             patch.object(ch, "_spawn_bg", side_effect=lambda coro: coro.close()), \
             patch.object(ch, "run_topic_turn", new=_fake_turn):
            # `new=`, non `side_effect=`: su una funzione async patch.object
            # costruisce un AsyncMock, e il side_effect girerebbe solo
            # all'await — che qui non arriva mai, perché `_spawn_bg` è finto
            out = await ch.channel_trigger_internal("SEAL-1", "ch", req)
        return out, visto

    async def test_the_caller_is_named_not_anonymous(self) -> None:
        out, visto = await self._trigger("clodia-primal")
        self.assertTrue(out["triggered"])
        self.assertEqual(out["by"], "clodia-primal")
        self.assertEqual(visto.get("trigger_author"), "clodia-primal")

    async def test_a_third_party_trigger_is_declared_external(self) -> None:
        out, _ = await self._trigger("clodia-primal")
        self.assertEqual(out["kind"], "external")

    async def test_the_turn_is_told_the_content_is_untrusted(self) -> None:
        """Mitigazione soft e dichiarata come tale: il taint vero è del gateway
        (sub-issue), ma il responder deve almeno SAPERE da dove arriva il testo."""
        _, visto = await self._trigger("clodia-primal")
        self.assertIn("clodia-primal", visto.get("directive", ""))
        self.assertIn("non fidato", visto.get("directive", "").lower())

    async def test_a_colony_agent_trigger_carries_no_warning(self) -> None:
        """Un agente registrato che innesca non è contenuto di terzi: nessun
        allarme, così l'avviso resta un segnale e non rumore di fondo."""
        out, visto = await self._trigger("clodia")
        self.assertEqual(out["kind"], "ai")
        self.assertEqual(visto.get("directive", ""), "")


class RunTopicTurnContextTests(unittest.IsolatedAsyncioTestCase):
    """La riga iniettata nel contesto di routing (`channels.py:2619`) diceva
    `author: channel, kind: human` per QUALUNQUE innesco."""

    async def _context_row(self, trigger_author: str | None) -> dict:
        visti: list[dict] = []
        meta = {"owner": "davide", "participants": ["clodia"], "tier": "SEAL-1"}

        def _compose(messages, config=None):
            visti.append(messages[-1])
            return "ctx"

        with patch.object(ch.registry, "get_by_name", side_effect=AGENTS.get), \
             patch.object(ch.topics_client, "list_messages", return_value=[]), \
             patch.object(ch.responder_routing, "compose_routing_context",
                          side_effect=_compose), \
             patch.object(ch, "_pick_responder", return_value=None):
            await ch.run_topic_turn("SEAL-1", "ch", meta, trigger_text="fai",
                                    principal_hint="channel",
                                    trigger_author=trigger_author)
        return visti[-1]

    async def test_a_proxy_trigger_enters_the_context_as_external(self) -> None:
        row = await self._context_row("clodia-primal")
        self.assertEqual(row["kind"], "external")
        self.assertEqual(row["author"], "clodia-primal")

    async def test_an_anonymous_trigger_is_external_too(self) -> None:
        """Fail-closed anche qui: `channel` non è un principal umano."""
        row = await self._context_row(None)
        self.assertEqual(row["kind"], "external")


if __name__ == "__main__":
    unittest.main()
