"""L'applicazione non pubblica più nessuna rotta hook (clodia-platform#223).

Step 3 di #222. Lo step 2 aveva chiuso la sola porta pubblica (`POST /hooks/{id}`,
issue #300) e la sua guardia viveva dentro `server/hooks/`: cancellando il modulo
sparirebbe anche l'invariante, che è il modo più silenzioso di riaprire una porta
— il test se ne va insieme al codice che sorvegliava, e nessuno vede il vuoto.

L'invariante si sposta quindi dove non dipende dal modulo: **l'app assemblata**.
Vale sia per la porta pubblica sia per le cinque rotte di gestione rimaste, che
amministravano record che nessuno poteva più invocare — elenco, creazione,
revoca, cancellazione, `ensure` interna e invocazione locale.

Se il caso «evento annunciato» tornerà (step 4 di #222), tornerà come qualcosa
che può postare e **non** può far partire un turno: allora questo test andrà
riscritto di proposito, che è esattamente la differenza fra una decisione e una
regressione.
"""
from __future__ import annotations

import unittest

from .. import main


class NoHookRouteTests(unittest.TestCase):
    def test_no_route_of_the_app_mentions_hooks(self) -> None:
        """Si guarda `openapi()`, non `app.routes`.

        Da FastAPI 0.115 in poi le rotte incluse non vengono appiattite in
        `app.routes`: restano dentro l'oggetto del router. Un test che filtra
        `app.routes` per path passa perché non trova **nessuna** rotta, non
        perché la superficie sia pulita — verificato qui prima di scriverlo,
        con le sei rotte hook ancora montate. `openapi()` risolve l'albero dei
        router ed è la stessa superficie che vede un chiamante.
        """
        paths = main.create_app().openapi().get("paths", {})
        self.assertEqual(sorted(p for p in paths if "hook" in p.lower()), [])


if __name__ == "__main__":
    unittest.main()
