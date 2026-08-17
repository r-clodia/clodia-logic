"""Inserire a mano una destinazione o una fonte: chi può, e cosa non passa.

    «devo poter inserire un egress o ingress anche a mano»
                                                — Davide, 17 ago 2026

Il dialog del gate resta la via giusta quando la destinazione la chiede un
agente, perché lì l'informazione è completa. Non è una via quando l'owner sa già
cosa censire: le 48 fonti del digest GRC non passano da 48 dialog.

Ciò che questi test fissano è il CONFINE, perché è la parte che si rompe in
silenzio: una voce in whitelist rende una destinazione muta per sempre, e una
fonte censita spegne il segnale sulla provenienza di tutto ciò che arriva da lì.
Se il controllo del ruolo cadesse, il sintomo non sarebbe un errore — sarebbe un
allarme che non suona più.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import observe


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(observe.router)
    return TestClient(app)


class _Risposta:
    """Minimo di `requests.Response` usato dall'endpoint."""

    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload
        self.content = b"x"

    def json(self) -> dict:
        return self._payload


class WhoCanEditTests(unittest.TestCase):
    def test_an_anonymous_request_is_unauthorized(self) -> None:
        with patch.object(observe, "_principal", return_value=None), \
             patch.object(observe, "_gw_post") as post:
            r = _app().post("/api/observe/whitelist/egress/allow",
                            json={"uri": "mailto:x@y.it"})
        self.assertEqual(401, r.status_code)
        post.assert_not_called()

    def test_a_non_admin_cannot_add_a_destination(self) -> None:
        """Concedere una destinazione è più privilegiato del singolo invio: chi
        può mandare una email non può per questo rendere muto quell'indirizzo."""
        with patch.object(observe, "_principal", return_value="ospite"), \
             patch("server.api.admin.is_admin", return_value=False), \
             patch.object(observe, "_gw_post") as post:
            r = _app().post("/api/observe/whitelist/egress/allow",
                            json={"uri": "mailto:x@y.it"})
        self.assertEqual(403, r.status_code)
        post.assert_not_called()

    def test_a_non_admin_cannot_remove_a_source_either(self) -> None:
        """Togliere è pericoloso nel verso opposto: rimuovere una fonte fidata fa
        tornare a contaminare — e nessuno lo noterebbe come un danno."""
        with patch.object(observe, "_principal", return_value="ospite"), \
             patch("server.api.admin.is_admin", return_value=False), \
             patch.object(observe, "_gw_post") as post:
            r = _app().post("/api/observe/whitelist/ingress/revoke",
                            json={"uri": "https://eur-lex.europa.eu/"})
        self.assertEqual(403, r.status_code)
        post.assert_not_called()

    def test_an_admin_reaches_the_gateway(self) -> None:
        with patch.object(observe, "_principal", return_value="davide"), \
             patch("server.api.admin.is_admin", return_value=True), \
             patch.object(observe, "_gw_post",
                          return_value=_Risposta(200, {"ok": True, "n": 4})) as post:
            r = _app().post("/api/observe/whitelist/ingress/allow",
                            json={"uri": "https://eur-lex.europa.eu/"})
        self.assertEqual(200, r.status_code)
        self.assertIs(True, r.json()["ok"])
        path, payload = post.call_args[0]
        self.assertEqual("/internal/egress/whitelist/ingress/allow", path)
        self.assertEqual({"uri": "https://eur-lex.europa.eu/"}, payload)


class WhatComesBackTests(unittest.TestCase):
    def test_an_empty_uri_never_reaches_the_gateway(self) -> None:
        with patch.object(observe, "_principal", return_value="davide"), \
             patch("server.api.admin.is_admin", return_value=True), \
             patch.object(observe, "_gw_post") as post:
            r = _app().post("/api/observe/whitelist/egress/allow", json={"uri": "   "})
        self.assertEqual(400, r.status_code)
        post.assert_not_called()

    def test_the_gateway_reason_is_forwarded_verbatim(self) -> None:
        """Il gateway sa PERCHÉ una voce è rifiutata («voce degenere», «schema
        della direzione sbagliata»). Tradurlo in «non valido» lascerebbe l'owner
        senza sapere come correggere — ed è l'unico modo che ha di capirlo,
        perché la regola vive nel gateway e non qui."""
        motivo = {"error": "voce degenere: 'https://' coprirebbe ogni host"}
        with patch.object(observe, "_principal", return_value="davide"), \
             patch("server.api.admin.is_admin", return_value=True), \
             patch.object(observe, "_gw_post", return_value=_Risposta(400, motivo)):
            r = _app().post("/api/observe/whitelist/egress/allow",
                            json={"uri": "https://"})
        self.assertEqual(400, r.status_code)
        self.assertEqual(motivo["error"], r.json()["error"])

    def test_an_unreachable_gateway_is_503_not_a_silent_success(self) -> None:
        """Il caso peggiore sarebbe un 200 senza che nulla sia stato scritto:
        l'owner crederebbe la fonte censita e continuerebbe a vederla contaminare
        senza collegare le due cose."""
        with patch.object(observe, "_principal", return_value="davide"), \
             patch("server.api.admin.is_admin", return_value=True), \
             patch.object(observe, "_gw_post", side_effect=OSError("connection refused")):
            r = _app().post("/api/observe/whitelist/egress/allow",
                            json={"uri": "mailto:x@y.it"})
        self.assertEqual(503, r.status_code)


if __name__ == "__main__":
    unittest.main()
