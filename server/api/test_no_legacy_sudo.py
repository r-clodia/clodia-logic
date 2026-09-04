"""Nessuna rotta `/api/sudo` pubblica (clodia-platform#295).

Il sudo legacy è stato sostituito dai GATE (`server/api/gate.py`): l'invariante
è che la sua superficie HTTP non torni, nemmeno per sbaglio.

La versione precedente di questo test ispezionava `app.routes` e **non poteva
fallire**. Da FastAPI 0.115 le rotte incluse con `include_router` non vengono
appiattite: restano dentro un oggetto wrapper senza `path`. Misurato sull'app
assemblata (FastAPI 0.141.1): 32 elementi in `app.routes`, di cui 26 wrapper di
router, contro 136 percorsi in `openapi()["paths"]` — il set che il test
costruiva conteneva in pratica solo `/docs`, `/redoc`, `/openapi.json` e
`/profile*`. Lo stesso difetto era già stato corretto in
`test_no_hook_surface.py`, che qui si prende a modello.

Il criterio: **un controllo che afferma un'assenza va scritto sulla stessa
vista da cui un chiamante vedrebbe la presenza.** Da qui i due controlli:

1. `openapi()["paths"]` — risolve l'albero dei router, è la superficie
   documentata che vede un client;
2. una richiesta HTTP vera — `openapi()` ha un suo punto cieco, una rotta
   montata con `include_in_schema=False` è raggiungibile ma non documentata.
"""
from __future__ import annotations

import unittest
from unittest import mock

from starlette.testclient import TestClient

from .. import main
from . import admin

# Le forme in cui il sudo legacy era esposto: la collezione e una sottorotta.
LEGACY_SUDO_PROBES = (
    ("GET", "/api/sudo"),
    ("POST", "/api/sudo"),
    ("GET", "/api/sudo/pending"),
)


class NoLegacySudoTests(unittest.TestCase):
    def test_no_documented_path_under_api_sudo(self) -> None:
        paths = main.create_app().openapi().get("paths", {})
        self.assertEqual(
            sorted(p for p in paths if p.startswith("/api/sudo")), [])

    def test_http_answers_404_on_legacy_sudo(self) -> None:
        """Si asserisce esattamente 404, non «diverso da 200».

        Prima del claim il gate di bootstrap risponde 423 a tutto ciò che non
        è nella allowlist, senza arrivare al routing: un controllo del tipo
        «non risponde 200» sarebbe verde per costruzione anche con la rotta
        montata. Il gate va quindi aperto, ed è l'unica cosa che si finge qui.
        """
        app = main.create_app()
        with mock.patch.object(admin, "is_initialized", return_value=True):
            client = TestClient(app)
            for method, path in LEGACY_SUDO_PROBES:
                with self.subTest(method=method, path=path):
                    self.assertEqual(
                        client.request(method, path).status_code, 404)


if __name__ == "__main__":
    unittest.main()
