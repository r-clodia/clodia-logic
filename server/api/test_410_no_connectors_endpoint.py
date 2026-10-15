"""L'app non pubblica più `/api/connectors` (clodia-platform#410, #520).

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
    def test_no_route_of_the_app_governs_connectors_globally(self) -> None:
        """La superficie che non deve tornare è `/api/connectors`: lo STATO dei
        connettori e il grant per-agente sulla credenziale.

        Il controllo era «nessuna rotta nomina `connector`», e da #520 sarebbe
        il controllo sbagliato: l'azione di un connettore su UN topic
        (`/api/topics/{tier}/{name}/connectors/{id}/{action}`) è un'altra cosa
        — è dentro una stanza, la autorizza l'owner di quella stanza, e non
        dice niente su quale agente può usare quale credenziale. Quello che
        resta vietato è il livello ISTANZA, che nasce nel gateway dove sta la
        vault e da lì si legge.
        """
        # `openapi()`, non `app.routes`: da FastAPI 0.115 le rotte incluse non
        # sono appiattite in `app.routes`, e il filtro passerebbe a vuoto.
        paths = main.create_app().openapi().get("paths", {})
        globali = [p for p in paths
                   if p.lower().startswith(("/api/connector", "/connector"))]
        self.assertEqual(sorted(globali), [])

    def test_the_connector_routes_that_exist_live_inside_a_topic(self) -> None:
        """L'altra metà: le rotte che nominano un connettore ci sono, e sono
        tutte sotto un topic. Senza, il test sopra resterebbe verde anche se
        qualcuno spostasse il governo dei connettori sotto un prefisso nuovo."""
        paths = main.create_app().openapi().get("paths", {})
        con_connettore = [p for p in paths if "connector" in p.lower()]
        self.assertTrue(con_connettore, "nessuna rotta di connettore: #520?")
        for p in con_connettore:
            with self.subTest(path=p):
                self.assertTrue(p.startswith("/api/topics/{tier}/{name}/"), p)

    def test_the_gateway_client_is_gone_too(self) -> None:
        self.assertIsNone(importlib.util.find_spec("server.api.connectors_client"))


if __name__ == "__main__":
    unittest.main()
