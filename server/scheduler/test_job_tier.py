"""Il tier di un job, e cosa succede se il provider non è conforme.

Decisione di Davide, 7 ago 2026: «un tier su job, se il provider dell'agente non
è conforme al tier richiesto il job fallisce con stato errore e messaggio di
errore loggato».

Un job è uno scope e dichiara il tier dei dati che tratterà. La SEAL effettiva di
un agente non è quella del suo seed ma quella del **provider** su cui gira (voce
13), perché è lì che il dato va: lo stesso agente è SEAL-3 su Scaleway e SEAL-1
su anthropic-api.

**Fallisce, non degrada.** Far girare su un provider più debole un job dichiarato
per dati SEAL-3 manderebbe quei dati dove non devono andare, e lo farebbe in
silenzio — il job risulterebbe eseguito con successo. Un run in errore si vede.

Tre casi in cui NON si rifiuta, e ognuno per una ragione diversa. Tier non
dichiarato: nessun requisito, ed è lo stato di ogni job esistente. Tier
illeggibile: un requisito che non sappiamo interpretare non è un requisito che
sappiamo far valere. Provider non risolto: lì il dubbio è NOSTRO, e rifiutare
sarebbe un guasto travestito da decisione — il difetto che è costato tre diagnosi
il 6 agosto.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import scheduler as S


def _clearance(valore):
    return patch("server.sdk_runtime.session._effective_clearance",
                 lambda kind: valore)


class ConformanceTests(unittest.TestCase):
    def test_a_conformant_provider_lets_the_job_run(self):
        with _clearance("SEAL-3"):
            self.assertIsNone(S._provider_not_conformant({"tier": "SEAL-3"}, "minerva"))

    def test_a_stronger_provider_is_conformant(self):
        """Il requisito è un minimo, non un'uguaglianza: un provider più
        sovrano tratta anche dati meno riservati."""
        with _clearance("SEAL-4"):
            self.assertIsNone(S._provider_not_conformant({"tier": "SEAL-2"}, "minerva"))

    def test_a_weaker_provider_refuses(self):
        with _clearance("SEAL-1"):
            self.assertIsNotNone(S._provider_not_conformant({"tier": "SEAL-3"}, "clodia"))

    def test_the_message_says_the_two_levels_and_the_two_remedies(self):
        """Un errore che dice solo «rifiutato» costringe a leggere il codice per
        capire cosa fare."""
        with _clearance("SEAL-1"):
            msg = S._provider_not_conformant({"tier": "SEAL-3"}, "clodia")
        self.assertIn("SEAL-3", msg)
        self.assertIn("SEAL-1", msg)
        self.assertIn("clodia", msg)
        self.assertIn("abbassa il tier", msg)


class SilenceTests(unittest.TestCase):
    """I tre casi in cui il controllo tace, ognuno per una ragione diversa."""

    def test_a_job_with_no_tier_has_no_requirement(self):
        """Lo stato di ogni job esistente. Rifiutarli in blocco al primo deploy
        non avrebbe protetto nulla: avrebbe spento lo scheduler."""
        with _clearance("SEAL-0"):
            self.assertIsNone(S._provider_not_conformant({}, "clodia"))
            self.assertIsNone(S._provider_not_conformant({"tier": ""}, "clodia"))

    def test_an_unreadable_tier_is_not_a_requirement_we_can_enforce(self):
        with _clearance("SEAL-0"):
            self.assertIsNone(S._provider_not_conformant({"tier": "altissimo"}, "clodia"))

    def test_an_unresolved_provider_does_not_refuse(self):
        """Qui il dubbio è nostro, non del job: rifiutare sarebbe un guasto
        travestito da decisione."""
        with _clearance(None):
            self.assertIsNone(S._provider_not_conformant({"tier": "SEAL-3"}, "clodia"))

    def test_an_exception_resolving_the_provider_does_not_refuse(self):
        def rotto(kind):
            raise RuntimeError("providers giù")

        with patch("server.sdk_runtime.session._effective_clearance", rotto):
            self.assertIsNone(S._provider_not_conformant({"tier": "SEAL-3"}, "clodia"))


class WiringTests(unittest.TestCase):
    def test_the_check_runs_before_the_chat_is_created(self):
        """Se scattasse dopo, lo spawn esisterebbe già e il turno potrebbe
        essere partito: il rifiuto arriverebbe a dati già in viaggio."""
        import inspect
        src = inspect.getsource(S.fire_job)
        self.assertLess(src.index("_provider_not_conformant"),
                        src.index("manager.create"))

    def test_the_refusal_is_recorded_as_a_failed_run(self):
        """«Fallisce con stato errore»: se non finisse nello storico, un job che
        non parte mai sembrerebbe un job che non è mai stato schedulato."""
        import inspect
        src = inspect.getsource(S.fire_job)
        blocco = src[src.index("_provider_not_conformant"):]
        self.assertIn("mark_run", blocco[:800])
        self.assertIn("error:", blocco[:800])

    def test_the_refusal_is_logged_as_an_error(self):
        import inspect
        src = inspect.getsource(S.fire_job)
        blocco = src[src.index("rifiuto = "):]
        self.assertIn("LOG.error", blocco[:600])


class PersistenceTests(unittest.TestCase):
    def test_the_field_is_part_of_the_job_schema(self):
        from . import db
        self.assertIn("tier", db._FIELDS)

    def test_an_empty_tier_clears_the_requirement(self):
        """`""` toglie, `None` non si pronuncia. Senza la distinzione un tier
        messo per errore non si potrebbe più rimuovere se non riscrivendo il
        file a mano."""
        import inspect
        from . import db
        src = inspect.getsource(db.update_job)
        self.assertIn("if tier is not None:", src)


if __name__ == "__main__":
    unittest.main()
