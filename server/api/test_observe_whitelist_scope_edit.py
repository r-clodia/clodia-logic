"""Aggiungere/togliere un egress/ingress LOCALE a un topic, dalla sidebar.

Richiesta di Davide, 18 set 2026: «devo poter aggiungere manualmente un
egress/ingress nella sidebar di canale». Dopo clodia-platform#374 (i BOT
hanno `topic.egress_add`/`ingress_add` nel pavimento, gated WALLS) mancava la
porta HTTP equivalente per un UMANO che clicca dalla webui — la globale
(`/api/observe/whitelist/{direction}/{action}`) è admin-only e non scopata.

Due guardie distinte, testate separatamente: `_require_scope_owner` (solo
l'owner di QUESTA stanza) e `require_authz_async` sul verbo MCP esatto (per un
umano, il gateway PDP decide sul ruolo admin — stessa porta che userebbe un
agente per lo stesso verbo).
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from . import observe


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(observe.router)
    return TestClient(app)


class _Risposta:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload
        self.content = b"x"

    def json(self) -> dict:
        return self._payload


_META = {"tier": "SEAL-1", "owner": "davide",
        "participants": {"davide": "owner", "avvocato": "contributor"}}


class VerbMappingTests(unittest.TestCase):
    def test_every_pair_maps_to_the_right_gate_verb(self) -> None:
        self.assertEqual(observe._SCOPE_VERB[("egress", "allow")], "topic.egress_add")
        self.assertEqual(observe._SCOPE_VERB[("egress", "revoke")], "topic.egress_remove")
        self.assertEqual(observe._SCOPE_VERB[("ingress", "allow")], "topic.ingress_add")
        self.assertEqual(observe._SCOPE_VERB[("ingress", "revoke")], "topic.ingress_remove")

    def test_an_unknown_pair_is_a_400_before_touching_anything(self) -> None:
        with patch("server.api.topics_client.async_open_topic") as apri:
            r = _app().post("/api/observe/whitelist/scope/SEAL-1/acme/egress/delete",
                            json={"uri": "gdrive:folder/1AbC"})
        self.assertEqual(400, r.status_code)
        apri.assert_not_called()


class WhoCanWriteTests(unittest.TestCase):
    def test_a_non_owner_participant_is_refused(self) -> None:
        """La stessa autorità di `drive_folder_add`: partecipare non basta,
        serve possedere lo scope."""
        with patch("server.api.topics_client.async_open_topic",
                   new=AsyncMock(return_value={"meta": _META})), \
             patch("server.api.channels._require_scope_owner",
                   side_effect=HTTPException(403, "riservato all'owner del topic")):
            r = _app().post("/api/observe/whitelist/scope/SEAL-1/acme/egress/allow",
                            json={"uri": "gdrive:folder/1AbC"})
        self.assertEqual(403, r.status_code)

    def test_the_owner_still_needs_the_admin_role_for_the_gated_verb(self) -> None:
        """`_require_scope_owner` decide CHI (owner di questa stanza);
        `require_authz_async` decide SE quel ruolo può eseguire una mutazione
        gated — le due non si sostituiscono a vicenda."""
        with patch("server.api.topics_client.async_open_topic",
                   new=AsyncMock(return_value={"meta": _META})), \
             patch("server.api.channels._require_scope_owner", return_value="davide"), \
             patch("server.api.channels.require_authz_async",
                   new=AsyncMock(side_effect=HTTPException(
                       403, "azione 'topic.egress_add' riservata agli admin"))), \
             patch.object(observe, "_gw_post") as gwp:
            r = _app().post("/api/observe/whitelist/scope/SEAL-1/acme/egress/allow",
                            json={"uri": "gdrive:folder/1AbC"})
        self.assertEqual(403, r.status_code)
        gwp.assert_not_called()

    def test_an_authorized_owner_reaches_the_gateway_with_the_right_path(self) -> None:
        with patch("server.api.topics_client.async_open_topic",
                   new=AsyncMock(return_value={"meta": _META})), \
             patch("server.api.channels._require_scope_owner", return_value="davide"), \
             patch("server.api.channels.require_authz_async",
                   new=AsyncMock(return_value="davide")), \
             patch.object(observe, "_gw_post",
                          return_value=_Risposta(200, {"ok": True, "added": True,
                                                       "uri": "gdrive:folder/1AbC"})) as gwp:
            r = _app().post("/api/observe/whitelist/scope/SEAL-1/acme/egress/allow",
                            json={"uri": "gdrive:folder/1AbC"})
        self.assertEqual(200, r.status_code)
        self.assertTrue(r.json()["added"])
        path, payload = gwp.call_args[0]
        self.assertEqual(
            "/internal/egress/whitelist/scope/SEAL-1/acme/egress/allow", path)
        self.assertEqual({"uri": "gdrive:folder/1AbC"}, payload)


class ValidationTests(unittest.TestCase):
    def test_a_missing_uri_is_a_400_before_reaching_the_gateway(self) -> None:
        with patch("server.api.topics_client.async_open_topic",
                   new=AsyncMock(return_value={"meta": _META})), \
             patch("server.api.channels._require_scope_owner", return_value="davide"), \
             patch("server.api.channels.require_authz_async",
                   new=AsyncMock(return_value="davide")), \
             patch.object(observe, "_gw_post") as gwp:
            r = _app().post("/api/observe/whitelist/scope/SEAL-1/acme/egress/allow", json={})
        self.assertEqual(400, r.status_code)
        gwp.assert_not_called()

    def test_a_gateway_rejection_forwards_its_reason(self) -> None:
        """Un URI degenere (`gdrive:folder/` senza id) è un errore
        dell'utente: il motivo del gateway arriva intatto, non un 500 generico."""
        with patch("server.api.topics_client.async_open_topic",
                   new=AsyncMock(return_value={"meta": _META})), \
             patch("server.api.channels._require_scope_owner", return_value="davide"), \
             patch("server.api.channels.require_authz_async",
                   new=AsyncMock(return_value="davide")), \
             patch.object(observe, "_gw_post",
                          return_value=_Risposta(400, {"error": "voce degenere"})):
            r = _app().post("/api/observe/whitelist/scope/SEAL-1/acme/egress/allow",
                            json={"uri": "gdrive:folder/"})
        self.assertEqual(400, r.status_code)
        self.assertEqual("voce degenere", r.json()["error"])

    def test_a_missing_topic_is_404(self) -> None:
        with patch("server.api.topics_client.async_open_topic",
                   new=AsyncMock(return_value=None)):
            r = _app().post("/api/observe/whitelist/scope/SEAL-1/fantasma/egress/allow",
                            json={"uri": "gdrive:folder/1AbC"})
        self.assertEqual(404, r.status_code)

    def test_the_gateway_being_unreachable_is_503_not_500(self) -> None:
        with patch("server.api.topics_client.async_open_topic",
                   new=AsyncMock(return_value={"meta": _META})), \
             patch("server.api.channels._require_scope_owner", return_value="davide"), \
             patch("server.api.channels.require_authz_async",
                   new=AsyncMock(return_value="davide")), \
             patch.object(observe, "_gw_post", side_effect=OSError("connection refused")):
            r = _app().post("/api/observe/whitelist/scope/SEAL-1/acme/egress/allow",
                            json={"uri": "gdrive:folder/1AbC"})
        self.assertEqual(503, r.status_code)


if __name__ == "__main__":
    unittest.main()
