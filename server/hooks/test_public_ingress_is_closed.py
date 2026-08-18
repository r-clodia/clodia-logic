"""La porta pubblica `POST /hooks/{id}` non esiste più (issue #300).

Step 2 di clodia-platform#222, ed è quello che toglie il rischio da solo.

Chi raggiungeva quella rotta iniettava un messaggio in una stanza e **faceva
partire un turno**, con un segreto copiabile per costruzione, accettato anche
nella **query string** — dove finisce nei log di accesso — e senza alcuna
identità firmata: l'autore era `hook:`, che descrive il tubo e non chi ha
parlato, e il principal era nullo. Sull'istanza viva: 8 hook registrati, `uses:
0` su tutti. Nessuno la stava usando.

Il resto del modulo (creazione, elenco, revoca, invocazione locale autenticata)
resta al suo posto: è lo step 3, e tenerli separati vuol dire che questo si può
revertire da solo se qualcosa che non abbiamo visto ci si appoggiava.

Il test guarda la **superficie**, non l'assenza di una funzione: l'invariante da
tenere è «questo modulo non pubblica rotte fuori da `/clodia/`», che resta vera
comunque la si riscriva. Se un chiamante ricomparirà, dovrà tornare come
qualcosa che può postare e **non** può innescare un turno.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import api, db


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(api.router)
    return app


class TheSurfaceHasNoPublicDoorTests(unittest.TestCase):
    def test_the_ingress_route_is_gone_from_the_openapi_surface(self) -> None:
        self.assertNotIn("/hooks/{hid}", _app().openapi().get("paths", {}))

    def test_no_route_of_this_module_lives_outside_the_authenticated_prefix(self) -> None:
        """L'invariante, non il singolo path: ogni rotta di questo modulo sta
        sotto `/clodia/`, cioè dietro l'autenticazione di piattaforma."""
        fuori = [r.path for r in api.router.routes
                 if not r.path.startswith("/clodia/")]
        self.assertEqual(fuori, [])


class NothingGetsInThroughItTests(unittest.TestCase):
    """Con il segreto GIUSTO in mano: la porta non c'è, e non c'è nemmeno il
    turno che ne nasceva."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(db, "_DIR", Path(tmp.name)),
                  patch.object(db, "_FILE", Path(tmp.name) / "hooks.json")):
            p.start()
            self.addCleanup(p.stop)
        self.turni: list[tuple] = []
        p = patch.object(api, "_queue_turn",
                         side_effect=lambda *a, **k: self.turni.append((a, k)) or True)
        p.start()
        self.addCleanup(p.stop)
        _, self.secret = db.create("SEAL-1", "acme", "acme", created_by="davide")
        self.client = TestClient(_app())

    def test_the_header_secret_no_longer_opens_anything(self) -> None:
        r = self.client.post("/hooks/acme", content=b"deploy fallito",
                             headers={"X-Hook-Secret": self.secret})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.turni, [])

    def test_the_secret_in_the_query_string_no_longer_opens_anything(self) -> None:
        """Era la variante peggiore: un segreto che finisce nei log di accesso
        di ogni proxy attraversato."""
        r = self.client.post(f"/hooks/acme?secret={self.secret}",
                             content=b"deploy fallito")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.turni, [])


class CreationDoesNotPublishAnAddressTests(unittest.IsolatedAsyncioTestCase):
    """Un indirizzo pubblicato che risponde 404 è peggio di un campo assente:
    chi lo riceve lo configura da qualche parte e scopre il guasto dopo, in un
    log di un sistema terzo."""

    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(db, "_DIR", Path(tmp.name)),
                  patch.object(db, "_FILE", Path(tmp.name) / "hooks.json"),
                  patch.object(api, "_require_chat_owner", return_value="davide")):
            p.start()
            self.addCleanup(p.stop)

    async def test_the_response_carries_no_path_and_no_url(self) -> None:
        class _Req:
            base_url = "https://clodia.example/"
            async def json(self):  # noqa: ANN202
                return {"label": "acme"}

        out = await api.create_hook("SEAL-1", "acme", _Req())
        self.assertIn("hook", out)
        self.assertNotIn("path", out)
        self.assertNotIn("url", out)


if __name__ == "__main__":
    unittest.main()
