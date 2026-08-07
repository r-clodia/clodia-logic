"""La clearance segue il provider di QUESTO spawn, non quello del seed.

Davide, 7 ago 2026: «il modello per un seed è sempre lo stesso ma può cambiare il
provider; tuttavia spawn diversi dello stesso seed possono girare su provider
diversi».

Vero, e il meccanismo esiste: `scoped_overrides.resolve()` ritorna `provider`. Ma
la clearance nel token si calcolava da `self.kind` — dal SEED — quindi uno spawn
spostato su un provider più debole conservava la clearance del seed.

**La direzione del danno è precisa.** Sposti uno spawn da un provider SEAL-3 a
uno SEAL-1: il token continua a dire SEAL-3, quello spawn apre un topic SEAL-3 e
ne manda i dati a un provider SEAL-1. È la dottrina della voce 13 — «nessuno
tratta dati SEAL-3+ su un provider SEAL-2-» — aggirata dal meccanismo che avrebbe
dovuto rispettarla.

**E il punto di chiamata aveva già l'override in mano**: lo usa due righe sopra
per il TTL e per `scoped_tools`. Non è un'informazione che mancava, è
un'informazione che nessuno portava fin lì — la stessa forma trovata sette volte
il 6 e 7 agosto, stavolta su un valore che protegge dati invece che azioni.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import session as S


SEAL = {"scaleway": "SEAL-3", "anthropic-api": "SEAL-1", "sovrano": "SEAL-4"}


def _env(provider_del_seed="scaleway"):
    return (patch.object(S, "agent_effective_provider", lambda k: provider_del_seed),
            patch("server.api.providers.provider_seal", lambda p: SEAL.get(p)))


class Base(unittest.TestCase):
    def run_with(self, ctx, fn):
        for c in ctx:
            c.start()
        try:
            return fn()
        finally:
            [c.stop() for c in ctx]


class OverrideTests(Base):
    def test_without_an_override_the_seed_provider_decides(self):
        self.run_with(_env(), lambda: self.assertEqual(
            S._effective_clearance("impiegato"), "SEAL-3"))

    def test_a_weaker_provider_lowers_the_clearance(self):
        """Il buco: prima restava SEAL-3 e i dati di un topic SEAL-3 sarebbero
        finiti su un provider SEAL-1."""
        self.run_with(_env(), lambda: self.assertEqual(
            S._effective_clearance("impiegato", {"provider": "anthropic-api"}),
            "SEAL-1"))

    def test_a_stronger_provider_raises_it(self):
        """L'altra direzione dev'essere vera per la stessa ragione: la clearance
        è del provider su cui il dato va, non una proprietà dell'agente."""
        self.run_with(_env(), lambda: self.assertEqual(
            S._effective_clearance("impiegato", {"provider": "sovrano"}),
            "SEAL-4"))

    def test_an_override_without_a_provider_changes_nothing(self):
        """Un override può portare solo il modello, o i tool."""
        self.run_with(_env(), lambda: self.assertEqual(
            S._effective_clearance("impiegato", {"model": "un-altro", "tools": []}),
            "SEAL-3"))

    def test_an_empty_provider_string_is_not_a_provider(self):
        self.run_with(_env(), lambda: self.assertEqual(
            S._effective_clearance("impiegato", {"provider": "   "}), "SEAL-3"))

    def test_no_override_at_all_is_accepted(self):
        self.run_with(_env(), lambda: self.assertEqual(
            S._effective_clearance("impiegato", None), "SEAL-3"))


class FallbackTests(Base):
    def test_an_unknown_provider_falls_back_to_the_declared_floor(self):
        """Non sapere il SEAL di un provider non deve produrre un token senza
        clearance: si ricade sulla minima dichiarata dal seed."""
        ctx = (patch.object(S, "agent_effective_provider", lambda k: "ignoto"),
               patch("server.api.providers.provider_seal", lambda p: None),
               patch.object(S, "_kind_clearance", lambda k: "SEAL-1"))
        self.run_with(ctx, lambda: self.assertEqual(
            S._effective_clearance("impiegato", {"provider": "ignoto"}), "SEAL-1"))

    def test_an_infrastructure_error_does_not_raise(self):
        """Questa funzione gira nel percorso di conio del token: un'eccezione qui
        è una sessione che non parte."""
        def rotto(p):
            raise RuntimeError("providers giù")
        ctx = (patch.object(S, "agent_effective_provider", lambda k: "scaleway"),
               patch("server.api.providers.provider_seal", rotto),
               patch.object(S, "_kind_clearance", lambda k: "SEAL-0"))
        self.run_with(ctx, lambda: self.assertEqual(
            S._effective_clearance("impiegato", {"provider": "scaleway"}), "SEAL-0"))


class WiringTests(unittest.TestCase):
    def test_every_mint_passes_the_spawns_override(self):
        """Se un solo punto di conio continuasse a passare il solo `kind`, il
        buco resterebbe aperto proprio lì — e sarebbe invisibile, perché gli
        altri tre sarebbero corretti."""
        import inspect
        import re
        src = inspect.getsource(S)
        nudi = re.findall(r'clearance=_effective_clearance\(self\.kind\)', src)
        self.assertEqual(nudi, [])

    def test_the_override_was_already_in_hand_at_those_call_sites(self):
        """La prova che non mancava un'informazione: lo stesso punto la usa già
        per il TTL."""
        import inspect
        src = inspect.getsource(S)
        self.assertIn("_runtime_token_ttl(", src)
        self.assertIn("_effective_clearance(self.kind, self._runtime_override)", src)


if __name__ == "__main__":
    unittest.main()
