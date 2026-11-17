"""#667 — the owner chooses the room's orchestrator with one click.

Owner, 11 Nov 2026: «vorrei un click che assegna il ruolo di orchestratore di
un canale … di default fosse Segretario, con la possibilità di riassegnarlo a
Clodia o Ophelia … solo un orchestratore attivo può esserlo». #695 (17 Nov
2026): any AI participant may be chosen; the three are the fallback order.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from . import channels, topics
from ..agents import coordinator

_URL = "/api/topics/SEAL-1/progetto/orchestrator"


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(topics.router)
    return TestClient(app)


class RouteTests(unittest.TestCase):
    def test_only_the_owner_chooses(self) -> None:
        with patch.object(topics, "_require_topic_owner",
                          side_effect=HTTPException(403, "solo l'owner")), \
             patch.object(topics.topics_client, "async_set_orchestrator",
                          new=AsyncMock()) as scrivi:
            r = _app().post(_URL, json={"orchestrator": "clodia"})
        self.assertEqual(403, r.status_code)
        scrivi.assert_not_called()

    def test_the_choice_is_signed_and_announced(self) -> None:
        post = AsyncMock()
        with patch.object(topics, "_require_topic_owner", return_value="davide"), \
             patch.object(topics, "_orchestrator_problem", return_value=""), \
             patch.object(topics.topics_client, "async_set_orchestrator",
                          new=AsyncMock(return_value={"orchestrator": "clodia",
                                                      "previous": None, "changed": True})) as scrivi, \
             patch.object(topics.topics_client, "async_open_topic", new=AsyncMock(
                 return_value={"meta": {"participants": ["clodia"], "orchestrator": "clodia"}})), \
             patch.object(channels, "orchestratore_da_meta", return_value="clodia"), \
             patch.object(topics.topics_client, "async_post_message", new=post):
            r = _app().post(_URL, json={"orchestrator": "Clodia", "by": "altri"})
        self.assertEqual(200, r.status_code)
        self.assertEqual(("clodia", "davide"),
                         (scrivi.await_args.args[2], scrivi.await_args.kwargs["by"]))
        self.assertEqual("clodia", r.json()["effective"])
        self.assertIn("clodia", post.await_args.args[3])

    def test_a_refusal_of_the_gateway_is_a_400(self) -> None:
        with patch.object(topics, "_require_topic_owner", return_value="davide"), \
             patch.object(topics, "_orchestrator_problem", return_value=""), \
             patch.object(topics.topics_client, "async_set_orchestrator",
                          new=AsyncMock(side_effect=ValueError("non è partecipante"))):
            r = _app().post(_URL, json={"orchestrator": "ophelia"})
        self.assertEqual(400, r.status_code)


class EffectiveTests(unittest.TestCase):
    def _eff(self, meta, eligible=("segretario", "clodia", "ophelia", "worker")):
        def spec(n):
            return type("S", (), {"name": n, "type": "bot"})() if n in eligible else None
        with patch.object(channels.registry, "get_by_name", side_effect=spec), \
                patch.object(channels, "_provider_seal_ok", return_value=True):
            return channels.orchestratore_da_meta(meta, "SEAL-1")

    def test_default_is_the_secretary(self) -> None:
        self.assertEqual("segretario", self._eff(
            {"participants": ["clodia", "segretario", "worker"]}))

    def test_the_room_choice(self) -> None:
        self.assertEqual("ophelia", self._eff(
            {"participants": ["clodia", "segretario", "ophelia"], "orchestrator": "ophelia"}))

    def test_a_choice_that_left_falls_back(self) -> None:
        self.assertEqual("segretario", self._eff(
            {"participants": ["segretario", "worker"], "orchestrator": "clodia"}))

    def test_without_the_secretary_clodia_leads(self) -> None:
        self.assertEqual("clodia", self._eff({"participants": ["clodia", "worker"]}))

    # ── #695: any AI participant ─────────────────────────────────────────
    def test_room_choice_keeps_any_seed(self) -> None:
        self.assertEqual("officer", coordinator.room_choice({"orchestrator": "Officer"}))
        self.assertIsNone(coordinator.room_choice({"orchestrator": "../x"}))
        self.assertIsNone(coordinator.room_choice({}))

    def test_a_specialist_chosen_leads(self) -> None:
        self.assertEqual("worker", self._eff(
            {"participants": ["segretario", "clodia", "worker"], "orchestrator": "worker"}))

    def test_a_person_chosen_falls_back(self) -> None:
        def spec(n):
            t = "human" if n == "davide" else "bot"
            return type("S", (), {"name": n, "type": t})()
        with patch.object(channels.registry, "get_by_name", side_effect=spec), \
                patch.object(channels, "_provider_seal_ok", return_value=True):
            self.assertEqual("segretario", channels.orchestratore_da_meta(
                {"participants": ["segretario", "davide"], "orchestrator": "davide"},
                "SEAL-1"))

    def test_a_removed_choice_goes_back_to_the_fallback_order(self) -> None:
        self.assertEqual("segretario", self._eff(
            {"participants": ["clodia", "segretario", "worker"]}))
        self.assertEqual("clodia", self._eff({"participants": ["clodia", "worker"]}))


class ChoiceCheckTests(unittest.TestCase):
    """#695: the route refuses a person, a proxy, an unknown name and a seed
    that cannot serve at the room's tier; any other AI participant passes."""

    def _problem(self, who, *, kind="bot", seal_ok=True, exists=True):
        spec = type("S", (), {"name": who, "type": kind})() if exists else None
        with patch.object(channels.registry, "get_by_name", return_value=spec), \
                patch.object(channels, "_provider_seal_ok", return_value=seal_ok), \
                patch.object(topics.topics_client, "open_topic",
                             return_value={"meta": {"tier": "SEAL-2"}}):
            return topics._orchestrator_problem("SEAL-2", "progetto", who)

    def test_an_ai_agent_passes(self) -> None:
        self.assertEqual("", self._problem("officer"))

    def test_a_person_or_proxy_is_refused(self) -> None:
        for kind in ("human", "proxy"):
            with self.subTest(kind=kind):
                self.assertIn("persona o un proxy", self._problem("davide", kind=kind))

    def test_unknown_and_ineligible_are_refused(self) -> None:
        self.assertIn("non è un agente", self._problem("ghost", exists=False))
        self.assertIn("SEAL-2", self._problem("officer", seal_ok=False))

    def test_the_route_refuses_before_writing(self) -> None:
        with patch.object(topics, "_require_topic_owner", return_value="davide"), \
                patch.object(topics, "_orchestrator_problem", return_value="no"), \
                patch.object(topics.topics_client, "async_set_orchestrator",
                             new=AsyncMock()) as scrivi:
            r = _app().post(_URL, json={"orchestrator": "davide"})
        self.assertEqual(400, r.status_code)
        scrivi.assert_not_called()

    def test_going_back_to_the_default_needs_no_check(self) -> None:
        with patch.object(topics, "_require_topic_owner", return_value="davide"), \
                patch.object(topics, "_orchestrator_problem",
                             side_effect=AssertionError("checked")), \
                patch.object(topics.topics_client, "async_set_orchestrator",
                             new=AsyncMock(return_value={"orchestrator": None,
                                                         "previous": "officer",
                                                         "changed": True})), \
                patch.object(topics.topics_client, "async_open_topic",
                             new=AsyncMock(return_value={"meta": {}})), \
                patch.object(channels, "orchestratore_da_meta", return_value="segretario"), \
                patch.object(topics.topics_client, "async_post_message", new=AsyncMock()):
            r = _app().post(_URL, json={"orchestrator": None})
        self.assertEqual(200, r.status_code)


class JobKnowledgeTests(unittest.TestCase):
    """#695: a seed without the `orchestrazione` skill learns the job from
    the turn's directive; the three fallback seeds carry it already."""

    def _caps(self, caps):
        return patch("server.agents.inheritance.effective_capabilities_of",
                     return_value=caps)

    def test_who_carries_the_skill(self) -> None:
        spec = type("S", (), {"name": "x"})()
        for caps, atteso in ((["base-pack/orchestrazione"], True), (["base-pack/*"], True),
                             (["*"], True), (["orchestrazione"], True),
                             (["it-pack/*", "base-pack/mail"], False), ([], False)):
            with self.subTest(caps=caps), self._caps(caps):
                self.assertIs(atteso, channels._has_capability(
                    spec, channels.ORCHESTRATION_SKILL))

    def test_the_brief_is_the_skill_without_front_matter(self) -> None:
        channels._ORCH_BRIEF.clear()
        self.addCleanup(channels._ORCH_BRIEF.clear)
        testo = channels._orchestration_brief()
        self.assertIn("Cosa fai da orchestratore", testo)
        self.assertNotIn("name: orchestrazione", testo)


if __name__ == "__main__":
    unittest.main()
