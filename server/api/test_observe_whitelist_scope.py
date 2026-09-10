"""Egress/ingress LOCALI di un topic, nella sidebar del topic — non la vista
d'istanza di `/api/observe/whitelist`.

Sidebar "Egress/Ingress" (9 set 2026, sostituisce il pannello Proxy): il
lettore legittimo qui non è "un umano autenticato qualunque" come per la
whitelist globale — è chi partecipa al topic. Stessa guardia di lettura dei
canali (`channels._require_member`).
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
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

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


_META = {"tier": "SEAL-1", "owner": "davide",
        "participants": {"davide": "owner", "avvocato": "contributor"}}


class WhoCanReadTests(unittest.TestCase):
    def test_an_anonymous_request_is_unauthorized(self) -> None:
        with patch.object(observe, "_principal", return_value=None), \
             patch.object(observe, "_gw") as gw:
            r = _app().get("/api/observe/whitelist/scope/SEAL-1/acme")
        self.assertEqual(401, r.status_code)
        gw.assert_not_called()

    def test_a_non_participant_cannot_read_it(self) -> None:
        """È la stanza di chi ci partecipa, non una vista d'istanza: un umano
        autenticato ma estraneo al topic non deve vedere il suo egress."""
        with patch.object(observe, "_principal", return_value="estraneo"), \
             patch("server.api.channels._principal_from_request", return_value="estraneo"), \
             patch("server.api.topics_client.async_open_topic",
                   new=AsyncMock(return_value={"meta": _META})), \
             patch.object(observe, "_gw") as gw:
            r = _app().get("/api/observe/whitelist/scope/SEAL-1/acme")
        self.assertEqual(403, r.status_code)
        gw.assert_not_called()

    def test_a_participant_reaches_the_gateway(self) -> None:
        with patch.object(observe, "_principal", return_value="avvocato"), \
             patch("server.api.channels._principal_from_request", return_value="avvocato"), \
             patch("server.api.topics_client.async_open_topic",
                   new=AsyncMock(return_value={"meta": _META})), \
             patch.object(observe, "_gw",
                          return_value=_Risposta(200, {"egress": ["gdrive:folder/1AbC"],
                                                       "ingress": []})) as gw:
            r = _app().get("/api/observe/whitelist/scope/SEAL-1/acme")
        self.assertEqual(200, r.status_code)
        self.assertEqual(["gdrive:folder/1AbC"], r.json()["egress"])
        path = gw.call_args[0][0]
        self.assertEqual("/internal/egress/whitelist/scope/SEAL-1/acme", path)

    def test_the_owner_reads_too(self) -> None:
        with patch.object(observe, "_principal", return_value="davide"), \
             patch("server.api.channels._principal_from_request", return_value="davide"), \
             patch("server.api.topics_client.async_open_topic",
                   new=AsyncMock(return_value={"meta": _META})), \
             patch.object(observe, "_gw",
                          return_value=_Risposta(200, {"egress": [], "ingress": []})):
            r = _app().get("/api/observe/whitelist/scope/SEAL-1/acme")
        self.assertEqual(200, r.status_code)


class DegradationTests(unittest.TestCase):
    """Best-effort: un guasto qui non deve rompere il resto della sidebar."""

    def test_a_missing_topic_is_404(self) -> None:
        with patch.object(observe, "_principal", return_value="davide"), \
             patch("server.api.topics_client.async_open_topic",
                   new=AsyncMock(return_value=None)):
            r = _app().get("/api/observe/whitelist/scope/SEAL-1/fantasma")
        self.assertEqual(404, r.status_code)

    def test_the_topic_service_being_down_is_503_not_500(self) -> None:
        async def boom(tier, name):
            raise RuntimeError("gateway muto")
        with patch.object(observe, "_principal", return_value="davide"), \
             patch("server.api.topics_client.async_open_topic", new=boom):
            r = _app().get("/api/observe/whitelist/scope/SEAL-1/acme")
        self.assertEqual(503, r.status_code)

    def test_the_egress_gateway_call_failing_degrades_to_empty_lists(self) -> None:
        """Non un errore che rompe la sidebar: il topic resta usabile, la
        sezione egress mostra solo che non c'è niente da mostrare adesso."""
        with patch.object(observe, "_principal", return_value="avvocato"), \
             patch("server.api.channels._principal_from_request", return_value="avvocato"), \
             patch("server.api.topics_client.async_open_topic",
                   new=AsyncMock(return_value={"meta": _META})), \
             patch.object(observe, "_gw", side_effect=OSError("connection refused")):
            r = _app().get("/api/observe/whitelist/scope/SEAL-1/acme")
        self.assertEqual(200, r.status_code)
        self.assertEqual({"egress": [], "ingress": []}, r.json())


if __name__ == "__main__":
    unittest.main()
