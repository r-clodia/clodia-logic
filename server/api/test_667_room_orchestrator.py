"""#667 — the owner chooses the room's orchestrator with one click.

Owner, 11 Nov 2026: «vorrei un click che assegna il ruolo di orchestratore di
un canale … di default fosse Segretario, con la possibilità di riassegnarlo a
Clodia o Ophelia … solo un orchestratore attivo può esserlo».
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

    def test_room_choice_ignores_other_seeds(self) -> None:
        self.assertIsNone(coordinator.room_choice({"orchestrator": "worker"}))


if __name__ == "__main__":
    unittest.main()
