"""L'app non pubblica più `/api/connectors` (clodia-platform#410).

Quelle due rotte proxavano il gateway su `/internal/connectors`, che nel gateway
non è mai stata registrata: 502 garantito a ogni chiamata, per costruzione, non
per un guasto d'ambiente. E anche se avesse risposto, governava il grant
per-agente sulla credenziale email — modello sostituito il 18/09 dalla whitelist
`inbox:`/`outbox:` per-scope («la domanda è DOVE, non CHI»). Non era una
funzione da riparare.

L'invariante sta sull'**app assemblata** e non dentro `server/api/connectors*`,
perché quei file non ci sono più: un test che vive nel modulo sorvegliato sparisce
insieme a lui, ed è il modo più silenzioso di far ricomparire una superficie.

Si controlla anche l'assenza del client: finché `connectors_client` resta
importabile, rimontare le rotte costa tre righe e in review sembra un ripristino.
Lo stato dei connettori si legge dal gateway, che ha la vault, via il verbo
`integrations.list` (clodia-tools 2.30.0).
"""
from __future__ import annotations

import importlib.util
import unittest

from .. import main


class NoConnectorsRouteTests(unittest.TestCase):
    def test_no_route_of_the_app_mentions_connectors(self) -> None:
        # `openapi()`, non `app.routes`: da FastAPI 0.115 le rotte incluse non
        # sono appiattite in `app.routes`, e il filtro passerebbe a vuoto.
        paths = main.create_app().openapi().get("paths", {})
        self.assertEqual(sorted(p for p in paths if "connector" in p.lower()), [])

    def test_the_gateway_client_is_gone_too(self) -> None:
        self.assertIsNone(importlib.util.find_spec("server.api.connectors_client"))


if __name__ == "__main__":
    unittest.main()
