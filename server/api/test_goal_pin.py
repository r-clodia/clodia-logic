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
    """Il pin non è un post-it: deve mettere qualcuno al lavoro.

    Il meta dice qual è l'obiettivo ADESSO e la stanza dice quando qualcuno
    l'ha cambiato — ma un obiettivo che nessuno raccoglie resta appeso finché
    una persona non si ricorda di chiedere. Le transizioni che cambiano il
    LAVORO da fare ingaggiano l'orchestratore con un turno vero; quelle che
    chiudono soltanto (unpin, `done`) lasciano una riga e basta.
    """

    _META = {"owner": "davide", "contact_agent": "clodia",
             "participants": {"davide": "owner", "clodia": "contributor"}}

    def _post(self, goal, ritorno, meta=None):
        with patch.object(topics, "_require_topic_owner", return_value="davide"), \
             patch.object(topics.topics_client, "async_set_goal",
                          new=AsyncMock(return_value=ritorno)), \
             patch.object(topics.topics_client, "async_open_topic",
                          new=AsyncMock(return_value={"meta": meta or self._META})), \
             patch.object(topics.topics_client, "async_post_message",
                          new=AsyncMock()) as riga, \
             patch("server.api.channels.post_channel_message",
                   new=AsyncMock(return_value={"posted": True})) as ingaggio:
            r = _app().post(_URL, json={"goal": goal})
        return r, riga, ingaggio

    def test_pinning_puts_the_orchestrator_to_work(self) -> None:
        """Il cuore del secondo giro: dal pin nasce un TURNO, non una riga."""
        r, riga, ingaggio = self._post(
            _GOAL, {"goal": dict(_GOAL, state="pinned", pinned_by="davide"),
                    "previous": None})
        self.assertEqual(200, r.status_code)
        riga.assert_not_awaited()
        testo = ingaggio.await_args.args[2]
        self.assertTrue(testo.startswith("@clodia "), testo[:40])
        self.assertIn("Portare il sito in produzione", testo)
        # L'ordine dice di FERMARSI prima dell'approvazione: qui si decide il
        # piano, non lo si attua.
        self.assertIn("strategy-review", testo)
        self.assertIn("Non eseguire niente", testo)
        self.assertTrue(ingaggio.await_args.kwargs["trusted_internal"])
        self.assertTrue(ingaggio.await_args.kwargs["skip_if_busy"])

    def test_approval_and_rejection_are_not_the_same_order(self) -> None:
        """La stessa destinazione, due significati opposti: dare lo stesso
        ordine a entrambe farebbe rieseguire da capo un piano già eseguito
        invece di correggerlo."""
        avanti = dict(_GOAL, state="in-progress", strategy_path="local/goals/s.md")
        _, _, approvazione = self._post(avanti, {"goal": avanti, "previous": "strategy-review"})
        _, _, correzione = self._post(avanti, {"goal": avanti, "previous": "claimed-done"})
        ok = approvazione.await_args.args[2]
        ko = correzione.await_args.args[2]
        self.assertIn("Strategia approvata", ok)
        self.assertIn("local/goals/s.md", ok)
        self.assertIn("non ha accettato l'esito", ko)
        self.assertIn("non ripartire da capo", ko)

    def test_closing_the_goal_does_not_burn_a_turn(self) -> None:
        """`done` e unpin non aprono lavoro: nessun agente da svegliare."""
        for goal, ritorno, atteso in (
            (dict(_GOAL, state="done"), {"goal": dict(_GOAL, state="done"), "previous": "claimed-done"}, "raggiunto"),
            (None, {"goal": None, "unpinned": True, "previous": "in-progress"}, "rimosso"),
        ):
            _, riga, ingaggio = self._post(goal, ritorno)
            ingaggio.assert_not_awaited()
            self.assertIn(atteso, riga.await_args.args[3].lower())

    def test_without_an_orchestrator_in_the_room_it_says_so(self) -> None:
        """Un obiettivo appeso a un agente che non c'è resterebbe fermo senza
        che niente lo dica: il caso va dichiarato, non subito."""
        meta = {"owner": "davide", "contact_agent": "clodia", "participants": {"davide": "owner"}}
        _, riga, ingaggio = self._post(
            _GOAL, {"goal": dict(_GOAL, state="pinned"), "previous": None}, meta=meta)
        ingaggio.assert_not_awaited()
        self.assertIn("non è fra i partecipanti", riga.await_args.args[3])

    def test_a_failed_announcement_does_not_fail_the_pin(self) -> None:
        """L'obiettivo è già scritto nel meta: far fallire la richiesta
        mostrerebbe come non riuscita un'operazione riuscita, e il prossimo
        click ripinnerebbe qualcosa che è già lì."""
        with patch.object(topics, "_require_topic_owner", return_value="davide"), \
             patch.object(topics.topics_client, "async_set_goal",
                          new=AsyncMock(return_value={"goal": dict(_GOAL, state="pinned")})), \
             patch.object(topics.topics_client, "async_open_topic",
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


class SkillTests(unittest.TestCase):
    """L'ordine del pin rimanda a una skill per il metodo. Se quella skill viene
    rinominata o tolta, l'orchestratore riceve il puntamento a un manuale che
    non esiste — e si vede solo dal vivo, su un obiettivo vero."""

    def test_the_order_points_to_a_skill_that_exists(self) -> None:
        import re
        from pathlib import Path
        ordine = topics._ordine_orchestratore(
            "davide", {"text": "x", "state": "pinned"}, None)
        citate = set(re.findall(r"skill `([a-z0-9-]+)`", ordine))
        self.assertTrue(citate, "l'ordine del pin non cita nessuna skill")
        base = Path(__file__).resolve().parents[2] / (
            "catalogs/packs/base-pack/plugins/base-pack/skills")
        presenti = {p.name for p in base.iterdir() if (p / "SKILL.md").is_file()}
        self.assertLessEqual(citate, presenti, f"skill citate ma assenti: {citate - presenti}")


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
