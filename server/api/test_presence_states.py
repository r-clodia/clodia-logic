"""Quattro stati di presenza, e le direzioni in cui devono sbagliare.

`here` sta guardando questa stanza · `elsewhere` è nella webui ma altrove ·
`background` ha la scheda dietro · `away` non c'è.

Quattro e non due perché le domande sono diverse: «mi legge adesso?» non è «è
raggiungibile?». Un indicatore solo le fonderebbe, e chi guarda dedurrebbe la
risposta sbagliata a una delle due — il caso peggiore è scrivere a qualcuno
credendolo davanti allo schermo perché è «online».

La direzione dell'errore conta: meglio dire `away` a chi c'è (si manda una
notifica in più) che `here` a chi non c'è (la notifica non parte e la persona
non sa di essere stata chiamata). Per questo il TTL è generoso ma la visibilità
è puntuale.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from . import presence as P


def _iso(secondi_fa: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=secondi_fa)).isoformat()


class _File:
    def __enter__(self):
        self._d = tempfile.TemporaryDirectory()
        self._p = patch.object(P, "_path", lambda: Path(self._d.name) / "presence.json")
        self._p.start()
        return self

    def __exit__(self, *a):
        self._p.stop()
        self._d.cleanup()
        return False

    def scrivi(self, d: dict):
        P._path().write_text(json.dumps(d), encoding="utf-8")


class TheFourStatesTests(unittest.TestCase):
    def test_here_when_watching_this_room_in_the_foreground(self):
        with _File() as f:
            f.scrivi({"giovanni|SEAL-1/acme": {"ts": _iso(2), "visible": True},
                      "giovanni|-": {"ts": _iso(2), "visible": True}})
            self.assertEqual(P.stato("giovanni", "SEAL-1", "acme"), "here")

    def test_elsewhere_when_in_the_webui_but_another_room(self):
        with _File() as f:
            f.scrivi({"giovanni|SEAL-1/altro": {"ts": _iso(2), "visible": True},
                      "giovanni|-": {"ts": _iso(2), "visible": True}})
            self.assertEqual(P.stato("giovanni", "SEAL-1", "acme"), "elsewhere")

    def test_background_when_the_tab_is_behind(self):
        """Anche se la stanza aperta è QUESTA: la scheda dietro significa che non
        sta leggendo, ed è la differenza fra «lo vede» e «lo troverà»."""
        with _File() as f:
            f.scrivi({"giovanni|SEAL-1/acme": {"ts": _iso(2), "visible": False},
                      "giovanni|-": {"ts": _iso(2), "visible": False}})
            self.assertEqual(P.stato("giovanni", "SEAL-1", "acme"), "background")

    def test_away_when_the_beat_is_old(self):
        with _File() as f:
            f.scrivi({"giovanni|-": {"ts": _iso(P.TTL_S + 30), "visible": True}})
            self.assertEqual(P.stato("giovanni", "SEAL-1", "acme"), "away")

    def test_away_when_there_is_nothing(self):
        with _File() as f:
            f.scrivi({})
            self.assertEqual(P.stato("chiunque", "SEAL-1", "acme"), "away")


class TheTtlIsGenerousOnPurposeTests(unittest.TestCase):
    """Una scheda in secondo piano viene STROZZATA dal browser: i timer scendono
    a uno al minuto. Un TTL stretto trasformerebbe «sta lavorando in un'altra
    finestra» in «se n'è andato» — e un indicatore che lampeggia mentre la
    persona è lì è un indicatore che si smette di guardare."""

    def test_a_beat_from_two_minutes_ago_still_counts(self):
        with _File() as f:
            f.scrivi({"giovanni|-": {"ts": _iso(120), "visible": False}})
            self.assertEqual(P.stato("giovanni", "SEAL-1", "acme"), "background")

    def test_the_ttl_survives_a_throttled_background_timer(self):
        self.assertGreater(P.TTL_S, 60, "sotto i 60s un tab in background sparisce")


class TheOldShapeStillReadsTests(unittest.TestCase):
    """Il file sta nella datadir CONDIVISA e i due container si aggiornano in
    momenti diversi: per qualche minuto uno scrive la forma nuova e l'altro
    legge. Rifiutare la vecchia significherebbe, in quella finestra, dichiarare
    assenti tutti — cioè una raffica di notifiche a gente presente."""

    def test_a_bare_iso_string_is_a_presence(self):
        with _File() as f:
            f.scrivi({"giovanni|SEAL-1/acme": _iso(3)})
            self.assertEqual(P.stato("giovanni", "SEAL-1", "acme"), "here")

    def test_and_an_old_one_is_not(self):
        with _File() as f:
            f.scrivi({"giovanni|SEAL-1/acme": _iso(P.TTL_S + 60)})
            self.assertEqual(P.stato("giovanni", "SEAL-1", "acme"), "away")


class WritingTests(unittest.TestCase):
    def test_a_beat_records_both_the_room_and_the_general_key(self):
        """Senza la chiave generale non si distingue «altrove nella webui» da
        «non c'è»: resterebbero due stati invece di quattro."""
        with _File():
            P.beat("giovanni", "SEAL-1/acme", True)
            d = json.loads(P._path().read_text())
        self.assertIn("giovanni|SEAL-1/acme", d)
        self.assertIn("giovanni|-", d)

    def test_a_beat_without_a_room_records_only_the_general_key(self):
        with _File():
            P.beat("giovanni", None, False)
            d = json.loads(P._path().read_text())
        self.assertEqual(list(d), ["giovanni|-"])
        self.assertFalse(d["giovanni|-"]["visible"])

    def test_touch_is_a_translation_not_a_second_writer(self):
        """Due scrittori con forme diverse sullo stesso file raccontano storie
        diverse appena una rete cade a metà."""
        import inspect
        self.assertIn("beat(", inspect.getsource(P.touch))

    def test_many_people_are_read_in_one_pass(self):
        with _File() as f:
            f.scrivi({"a|-": {"ts": _iso(1), "visible": True},
                      "b|SEAL-1/acme": {"ts": _iso(1), "visible": True}})
            letture = []
            vero = P._load

            def _conta():
                letture.append(1)
                return vero()

            with patch.object(P, "_load", _conta):
                s = P.stati(["a", "b", "c"], "SEAL-1", "acme")
        self.assertEqual(s, {"a": "elsewhere", "b": "here", "c": "away"})
        self.assertEqual(len(letture), 1, "un file riletto una volta per persona")


if __name__ == "__main__":
    unittest.main()
