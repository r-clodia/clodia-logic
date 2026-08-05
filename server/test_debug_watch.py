"""Modalità debug: rilevazione delle anomalie e chiamata al guardiano.

Il difetto che questa modalità chiude ha una forma sola, vista tre volte in un
giorno: l'informazione sul guasto ESISTE e non arriva a chi può agire. Un turno
che solleva scrive nel log e lascia il canale muto — dall'esterno «l'ho
menzionato e non risponde». Una delega verso un agente non idoneo fa `continue`.
Un provider non connesso ritorna False.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import debug_watch as dw


class EnabledTests(unittest.TestCase):
    def test_it_is_off_by_default(self):
        """Accesa costa: il guardiano entra e ogni anomalia sveglia un turno.
        Un default acceso diventerebbe permanente per inerzia."""
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(dw.enabled())

    def test_the_usual_truthy_spellings_work(self):
        for v in ("1", "true", "on", "YES", "True"):
            with self.subTest(v=v), patch.dict("os.environ", {"CLODIA_DEBUG_MODE": v}):
                self.assertTrue(dw.enabled())

    def test_anything_else_is_off(self):
        for v in ("0", "", "no", "forse"):
            with self.subTest(v=v), patch.dict("os.environ", {"CLODIA_DEBUG_MODE": v}):
                self.assertFalse(dw.enabled())


class AntiLoopTests(unittest.TestCase):
    def setUp(self):
        dw.reset_dedup()

    def test_the_watcher_is_never_the_subject(self):
        """Un fallimento del guardiano non deve svegliare il guardiano: è la
        ricorsione che trasforma un guasto in una tempesta."""
        a = dw.Anomaly(kind="turn_failed", channel="SEAL-1/x", subject=dw.WATCHER)
        self.assertFalse(dw.should_report(a))

    def test_the_same_fault_is_reported_once_per_window(self):
        """Lo stesso guasto si ripete a ogni tentativo. Senza soppressione un
        provider giù riempirebbe il canale di segnalazioni identiche — cioè il
        rumore che rende inutile un allarme."""
        mk = lambda: dw.Anomaly(kind="no_provider", channel="SEAL-1/x", subject="avvocato")
        self.assertTrue(dw.should_report(mk()))
        self.assertFalse(dw.should_report(mk()))
        self.assertFalse(dw.should_report(mk()))

    def test_a_different_agent_is_a_different_fault(self):
        self.assertTrue(dw.should_report(
            dw.Anomaly(kind="no_provider", channel="SEAL-1/x", subject="avvocato")))
        self.assertTrue(dw.should_report(
            dw.Anomaly(kind="no_provider", channel="SEAL-1/x", subject="commercialista")))

    def test_a_different_channel_is_a_different_fault(self):
        self.assertTrue(dw.should_report(
            dw.Anomaly(kind="turn_failed", channel="SEAL-1/a", subject="clodia")))
        self.assertTrue(dw.should_report(
            dw.Anomaly(kind="turn_failed", channel="SEAL-1/b", subject="clodia")))

    def test_the_window_expires(self):
        import time
        mk = lambda: dw.Anomaly(kind="turn_failed", channel="SEAL-1/x", subject="clodia")
        self.assertTrue(dw.should_report(mk()))
        # `later` calcolato PRIMA della patch: una lambda che chiama time.time()
        # mentre la sta sostituendo ricorre su sé stessa.
        later = time.time() + dw.DEDUP_SECONDS + 1
        with patch.object(dw.time, "time", lambda: later):
            self.assertTrue(dw.should_report(mk()))


class BriefTests(unittest.TestCase):
    """Il brief deve portare ENTRAMBE le uscite, e l'evidenza."""

    def _brief(self, **kw):
        return dw.Anomaly(kind="turn_failed", channel="SEAL-1/bilancio",
                          subject="avvocato", detail="il turno è morto",
                          evidence=kw).brief()

    def test_it_offers_repair_and_filing_as_distinct_outcomes(self):
        """Chiedere solo di riparare produce tentativi improvvisati quando la
        causa è nel codice; chiedere solo di aprire una issue rimanda anche ciò
        che si sistemava con un restart."""
        b = self._brief(error="CLIConnectionError")
        self.assertIn("runtime.restart_agent", b)
        self.assertIn("github.issue_write", b)
        self.assertIn("clodia-platform", b)

    def test_it_carries_the_evidence(self):
        b = self._brief(error="Timeout", hop=1)
        self.assertIn("Timeout", b)
        self.assertIn("error=", b)

    def test_it_names_channel_and_subject(self):
        b = self._brief()
        self.assertIn("SEAL-1/bilancio", b)
        self.assertIn("avvocato", b)

    def test_empty_evidence_does_not_produce_a_dangling_label(self):
        self.assertIn("Evidenza: —", self._brief())

    def test_it_tells_the_watcher_not_to_ask_before_looking(self):
        """È il comportamento da correggere: tre agenti hanno concluso «guasto
        tecnico, serve un intervento» senza che nessuno avesse guardato. Chi ha
        gli strumenti guarda prima di chiedere."""
        b = self._brief()
        self.assertIn("prima di aver guardato", b)


if __name__ == "__main__":
    unittest.main()
