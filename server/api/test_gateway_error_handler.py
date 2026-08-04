"""Un gateway irraggiungibile non è un 500 (trovato dal vivo il 4 ago 2026).

Durante il riavvio del gateway un endpoint che non catturava `TopicsClientError`
ha fatto uscire l'eccezione, e la UI ha mostrato «HTTP 500 — Internal Server
Error»: il messaggio che non dice niente e non dice cosa fare.

I punti che catturano quell'eccezione sono decine, e ne basta uno che dimentica.
L'handler globale è l'unico modo per cui non può essere dimenticato — ed è la
ragione per cui questo test esiste al livello dell'app e non dell'endpoint.
"""
from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from .topics_client import TopicsClientError


def _app() -> FastAPI:
    """Replica l'handler registrato in `create_app`, senza avviare l'istanza."""
    app = FastAPI()

    @app.exception_handler(TopicsClientError)
    async def _h(_request, exc: TopicsClientError):  # noqa: ANN202
        if exc.is_client_error:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status or 400)
        unreachable = "irraggiungibile" in str(exc)
        return JSONResponse({"detail": (
            "gateway dei topic non raggiungibile — probabilmente si sta "
            "riavviando. Riprova fra qualche secondo." if unreachable
            else f"errore dal gateway dei topic: {exc.detail[:200]}")},
            status_code=503 if unreachable else 502)

    @app.get("/boom")
    def boom():  # noqa: ANN201
        raise TopicsClientError("gateway topics irraggiungibile: Connection refused")

    @app.get("/refused")
    def refused():  # noqa: ANN201
        raise TopicsClientError("gateway remote → HTTP 400: motivo chiaro",
                                status=400, detail="motivo chiaro")

    @app.get("/broken")
    def broken():  # noqa: ANN201
        raise TopicsClientError("gateway topics → HTTP 500: boom", status=500,
                                detail="boom")

    return app


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(_app(), raise_server_exceptions=False)

    def test_unreachable_is_503_with_what_to_do(self):
        r = self.c.get("/boom")
        self.assertEqual(r.status_code, 503)
        # 503 e non 502: non ha risposto male, non ha risposto — ed è transitorio
        self.assertIn("Riprova", r.json()["detail"])

    def test_a_client_error_keeps_its_status_and_text(self):
        r = self.c.get("/refused")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], "motivo chiaro")

    def test_a_gateway_5xx_is_502_with_the_reason(self):
        r = self.c.get("/broken")
        self.assertEqual(r.status_code, 502)
        self.assertIn("boom", r.json()["detail"])

    def test_nothing_leaks_as_a_bare_500(self):
        for path in ("/boom", "/refused", "/broken"):
            with self.subTest(path=path):
                self.assertNotEqual(self.c.get(path).status_code, 500)


if __name__ == "__main__":
    unittest.main()
