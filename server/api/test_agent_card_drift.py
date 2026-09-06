"""La divergenza si vede dove si guardano i verbi (clodia-platform#203).

Il log da solo non basta: se la prima rotazione se lo porta via, di una
divergenza non resta niente. E i tre guasti che hanno prodotto l'issue erano
tutti silenziosi — `ophelia` con un `*` sopravvissuto al proprio ritiro si
vedeva solo aprendo un file che nessuno apre.

La scheda dell'agente è il posto in cui qualcuno guarda già cosa un agente può
fare: è lì che una dichiarazione rimasta indietro deve comparire.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from . import agent_registry as AR
from . import gateway_admin


VERBI = {"agent": "avvocato", "verbs": [], "groups": []}


def _card(spec, registrazione, verbi=None):
    async def _verbs_async(_n):
        if isinstance(verbi, Exception):
            raise verbi
        return dict(verbi or VERBI)

    async def _reg_async(_n):
        if isinstance(registrazione, Exception):
            raise registrazione
        return registrazione

    with mock.patch.object(AR, "registry", SimpleNamespace(get_by_name=lambda n: spec)), \
            mock.patch.object(gateway_admin, "agent_verbs_async", _verbs_async), \
            mock.patch.object(gateway_admin, "registration_async", _reg_async):
        r = asyncio.run(AR.get_agent_verbs("avvocato", None))
    return json.loads(r.body)


def _seed(**kw):
    base = dict(name="avvocato", type="normal", tool_permissions=["topic.open"],
                gated_tools=None, denied_tools=None, profile_tools=None)
    base.update(kw)
    return SimpleNamespace(**base)


class CardDriftTests(unittest.TestCase):
    def test_a_divergence_appears_on_the_card(self):
        b = _card(_seed(denied_tools=["email.send"]),
                  {"agent": "avvocato", "registered": True,
                   "allowed_tools": ["topic.open"], "gated_tools": [],
                   "denied_tools": [], "profile_tools": []})
        self.assertEqual(b["drift"]["status"], "diverged")
        self.assertEqual(b["drift"]["fields"][0]["field"], "denied_tools")

    def test_an_aligned_seed_adds_nothing(self):
        """Una riga «tutto a posto» su ogni scheda è rumore, e il rumore è come
        muore un segnale."""
        b = _card(_seed(), {"agent": "avvocato", "registered": True,
                            "allowed_tools": ["topic.open"], "gated_tools": [],
                            "denied_tools": [], "profile_tools": []})
        self.assertNotIn("drift", b)

    def test_an_unregistered_agent_is_shown_as_such(self):
        b = _card(_seed(), {"agent": "avvocato", "registered": False})
        self.assertEqual(b["drift"]["status"], "unregistered")

    def test_the_card_survives_a_drift_that_fails(self):
        """Best-effort dentro un best-effort: la scheda esiste per mostrare i
        verbi, e un rilevatore che la fa sparire insegna a non fidarsi del
        pannello — il danno peggiore in una vista di sicurezza."""
        b = _card(_seed(), RuntimeError("gateway giù"))
        self.assertEqual(b["verbs"], [])
        self.assertEqual(b["drift"]["status"], "unavailable")

    def test_a_human_has_no_drift_row(self):
        """Un umano non sta nella config del gateway per disegno: mostrargli una
        divergenza direbbe che gli manca qualcosa che non deve avere."""
        b = _card(_seed(type="human"), {"agent": "avvocato", "registered": False})
        self.assertNotIn("drift", b)

    def test_the_verbs_still_come_first(self):
        """Il drift si aggiunge, non sostituisce: la scheda resta quella."""
        b = _card(_seed(), {"agent": "avvocato", "registered": False},
                  verbi={"agent": "avvocato", "verbs": [{"verb": "topic.open"}],
                         "groups": []})
        self.assertEqual(b["verbs"], [{"verb": "topic.open"}])


if __name__ == "__main__":
    unittest.main()
