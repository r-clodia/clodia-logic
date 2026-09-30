"""Fissare un messaggio dell'utente come OBIETTIVO del canale.

clodia-platform#457. Il pin promuove una richiesta a requisito vincolante: da
lì in poi l'orchestratore deve portarla a termine, e togliere il pin è l'atto
che ferma l'esecuzione della strategia.

Le due proprietà che questi test difendono:
  - **è dell'owner**: un partecipante non può appendere al canale un lavoro che
    impegna gli agenti finché resta lì;
  - **`pinned_by` non arriva dal client**: il gateway si fida del campo `by`
    perché non conosce i ruoli umani di uno scope, quindi questo servizio deve
    metterci il principal che ha appena verificato, mai quello che gli viene
    suggerito nel corpo della richiesta.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from . import topics


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(topics.router)
    return TestClient(app)


_URL = "/api/topics/SEAL-1/progetto/goal"
_GOAL = {"text": "Portare il sito in produzione", "message_id": "m1"}


class OwnerTests(unittest.TestCase):
    def test_only_the_owner_can_pin_a_goal(self) -> None:
        """La guardia scatta PRIMA di toccare il gateway: un 403 che arriva dopo
        aver già scritto non è una guardia."""
        with patch.object(topics, "_require_topic_owner",
                          side_effect=HTTPException(403, "solo l'owner")), \
             patch.object(topics.topics_client, "async_set_goal",
                          new=AsyncMock()) as scrivi:
            r = _app().post(_URL, json={"goal": _GOAL})
        self.assertEqual(403, r.status_code)
        scrivi.assert_not_called()

    def test_the_verified_principal_signs_the_pin(self) -> None:
        with patch.object(topics, "_require_topic_owner", return_value="davide"), \
             patch.object(topics.topics_client, "async_set_goal",
                          new=AsyncMock(return_value={"goal": dict(_GOAL, pinned_by="davide")})) as scrivi, \
             patch.object(topics.topics_client, "async_post_message", new=AsyncMock()):
            # `pinned_by` suggerito dal client: deve essere ignorato, il campo
            # che conta è l'argomento `by`.
            r = _app().post(_URL, json={"goal": dict(_GOAL, pinned_by="qualcun-altro")})
        self.assertEqual(200, r.status_code)
        self.assertEqual("davide", scrivi.await_args.kwargs["by"])

    def test_unpin_passes_a_null_goal_through(self) -> None:
        with patch.object(topics, "_require_topic_owner", return_value="davide"), \
             patch.object(topics.topics_client, "async_set_goal",
                          new=AsyncMock(return_value={"goal": None, "unpinned": True})) as scrivi, \
             patch.object(topics.topics_client, "async_post_message", new=AsyncMock()):
            r = _app().post(_URL, json={"goal": None})
        self.assertEqual(200, r.status_code)
        self.assertIsNone(r.json()["goal"])
        self.assertIsNone(scrivi.await_args.args[2])


class AnnuncioTests(unittest.TestCase):
    """Il meta dice qual è l'obiettivo ADESSO; la stanza dice quando qualcuno
    l'ha cambiato. Senza la riga di sistema, un lavoro che si ferma non ha
    nessuna traccia del perché."""

    def _post(self, goal, ritorno):
        with patch.object(topics, "_require_topic_owner", return_value="davide"), \
             patch.object(topics.topics_client, "async_set_goal",
                          new=AsyncMock(return_value=ritorno)), \
             patch.object(topics.topics_client, "async_post_message",
                          new=AsyncMock()) as annuncia:
            r = _app().post(_URL, json={"goal": goal})
        return r, annuncia

    def test_pin_announces_the_goal_in_the_room(self) -> None:
        r, annuncia = self._post(_GOAL, {"goal": dict(_GOAL, pinned_by="davide")})
        self.assertEqual(200, r.status_code)
        testo = annuncia.await_args.args[3]
        self.assertIn("Portare il sito in produzione", testo)
        self.assertIn("davide", testo)
        self.assertEqual("system", annuncia.await_args.kwargs["kind"])

    def test_unpin_announces_that_execution_stops(self) -> None:
        r, annuncia = self._post(None, {"goal": None, "unpinned": True})
        self.assertIn("rimosso", annuncia.await_args.args[3].lower())

    def test_a_failed_announcement_does_not_fail_the_pin(self) -> None:
        """L'obiettivo è già scritto nel meta: far fallire la richiesta
        mostrerebbe come non riuscita un'operazione riuscita, e il prossimo
        click ripinnerebbe qualcosa che è già lì."""
        with patch.object(topics, "_require_topic_owner", return_value="davide"), \
             patch.object(topics.topics_client, "async_set_goal",
                          new=AsyncMock(return_value={"goal": dict(_GOAL)})), \
             patch.object(topics.topics_client, "async_post_message",
                          new=AsyncMock(side_effect=RuntimeError("gateway giù"))):
            r = _app().post(_URL, json={"goal": _GOAL})
        self.assertEqual(200, r.status_code)


class GatewayErrorTests(unittest.TestCase):
    def test_a_gateway_refusal_is_not_a_500(self) -> None:
        errore = topics.topics_client.TopicsClientError("gateway set-goal → HTTP 400")
        with patch.object(topics, "_require_topic_owner", return_value="davide"), \
             patch.object(topics.topics_client, "async_set_goal",
                          new=AsyncMock(side_effect=errore)), \
             patch.object(topics.topics_client, "async_post_message",
                          new=AsyncMock()) as annuncia:
            r = _app().post(_URL, json={"goal": _GOAL})
        self.assertEqual(502, r.status_code)
        annuncia.assert_not_called()  # niente annuncio di ciò che non è successo


class GrantTests(unittest.TestCase):
    """Chi orchestra deve poter dichiarare a che punto è l'obiettivo, e NON
    poterselo fissare da sé: `topic.goal_progress` sì, nessun verbo di pin."""

    def test_the_orchestrator_can_advance_but_not_pin(self) -> None:
        import yaml
        from pathlib import Path
        base = Path(__file__).resolve().parents[2] / "catalogs/packs/base-pack/agents"
        grant = yaml.safe_load((base / "clodia/agent.yaml").read_text(encoding="utf-8"))
        verbi = set(grant.get("tool_permissions") or [])
        self.assertIn("topic.goal_progress", verbi)
        self.assertNotIn("topic.set_goal", verbi)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
