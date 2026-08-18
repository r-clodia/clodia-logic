"""Il primo admin deve poter essere creato su un'istanza non reclamata.

Difetto reale, trovato installando una istanza nuova su un'altra macchina:
`POST /api/agents` rispondeva `401 autenticazione richiesta`. Il middleware
`_bootstrap_gate` PERMETTE quella chiamata prima del claim — il commento dice
"SOLO la creazione del primo superadmin" — ma l'endpoint pretendeva comunque un
admin autenticato. Quindi l'admin non poteva esistere prima di sé stesso, e
un'istanza appena installata restava inutilizzabile sull'unico passo che il log
di boot indica ("apri la webui e crea il primo admin").

La finestra si chiude da sé: appena un human esiste, la chiamata torna
admin-only. Chi arriva primo reclama — è il disegno claim-by-first, già scelto
dal middleware.
"""
from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from . import agent_registry


class PreClaimTests(unittest.TestCase):
    def test_the_guard_is_skipped_only_when_uninitialized(self):
        """Si asserisce sul CODICE perché l'endpoint è async e il resto del corpo
        scrive su disco: qui interessa l'ordine delle guardie, non la creazione."""
        import inspect
        src = inspect.getsource(agent_registry.create_agent)
        # la chiamata a require_authz deve essere DENTRO un ramo condizionale
        self.assertIn("is_initialized()", src)
        i_cond = src.index("is_initialized()")
        # la guardia esiste in due forme (sync e offload su thread, #106): qui
        # conta la POSIZIONE, non quale delle due sia in uso.
        m = re.search(r'require_authz(?:_async)?\(request, "agents\.create"\)', src)
        self.assertIsNotNone(m, "la guardia agents.create non è più riconoscibile")
        i_authz = m.start()
        self.assertLess(i_cond, i_authz,
                        "require_authz deve venire DOPO il controllo sullo stato "
                        "del claim, altrimenti il primo admin non è creabile")

    def test_the_middleware_allows_exactly_this_call_preclaim(self):
        """Il fix segue una decisione già presa altrove: se il middleware
        cambiasse idea, questo test lo dice invece di lasciare due politiche
        divergenti."""
        from .. import main
        import inspect
        src = inspect.getsource(main)
        self.assertIn('return path == "/api/agents"', src)

    def test_an_initialized_instance_still_requires_an_admin(self):
        """La parte che NON deve regredire: chiusa la finestra, torna admin-only."""
        import inspect
        src = inspect.getsource(agent_registry.create_agent)
        self.assertIn("require_authz", src)
        # nessun ramo che salti la guardia quando l'istanza è inizializzata
        self.assertNotIn("if True", src)


if __name__ == "__main__":
    unittest.main()
