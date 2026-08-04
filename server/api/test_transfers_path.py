"""Risoluzione del `dest` di un transfer dentro lo scratch della sessione.

Guasto reale (bilancio-tomato-2026): `topic.fetch` su uno zip di 430KB tornava
`400 Bad Request`, e tre agenti hanno concluso — ognuno per conto proprio — che il
servizio era guasto e serviva un intervento infrastrutturale. Non era guasto.

Due difetti sommati:
- un `dest` relativo veniva risolto contro la cwd del processo agent-server, cioè
  fuori dallo scratch per costruzione;
- la cwd dell'AGENTE è la radice dello spawn, mentre lo scratch è
  `<spawn>/scratch`: un path composto da `pwd` è plausibile e sbagliato, che è il
  modo peggiore di essere sbagliato.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from . import transfers


class ScratchPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spawn_root = Path(self.tmp.name) / "commercialista-1"
        self.scratch = self.spawn_root / "scratch"
        self.scratch.mkdir(parents=True)
        self.spawn = SimpleNamespace(scratch=self.scratch,
                                     dir=SimpleNamespace(name="commercialista-1"))

    def test_a_bare_filename_lands_in_the_scratch(self):
        """È ciò che un agente scrive naturalmente, e prima era un 400."""
        got = transfers._scratch_path(self.spawn, "estratto.zip")
        self.assertEqual(got, (self.scratch / "estratto.zip").resolve())

    def test_a_relative_subpath_lands_in_the_scratch(self):
        got = transfers._scratch_path(self.spawn, "cedolini/luglio.pdf")
        self.assertEqual(got, (self.scratch / "cedolini/luglio.pdf").resolve())

    def test_an_absolute_path_inside_the_scratch_is_kept(self):
        target = self.scratch / "x.zip"
        self.assertEqual(transfers._scratch_path(self.spawn, str(target)),
                         target.resolve())

    def test_the_spawn_root_is_refused_and_the_message_says_where_the_scratch_is(self):
        """IL caso che ha bruciato una giornata: la cwd dell'agente è QUESTA
        cartella, un livello sopra lo scratch. Il rifiuto deve dire dov'è lo
        scratch e che la cwd non lo è, altrimenti l'agente rilegge `pwd` e
        ritenta identico."""
        with self.assertRaises(HTTPException) as cm:
            transfers._scratch_path(self.spawn, str(self.spawn_root / "estratto.zip"))
        self.assertEqual(cm.exception.status_code, 400)
        d = cm.exception.detail
        self.assertIn(str(self.scratch), d)
        self.assertIn("cwd", d)
        self.assertIn("estratto.zip", d)

    def test_an_escape_with_dotdot_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            transfers._scratch_path(self.spawn, "../../etc/passwd")
        self.assertEqual(cm.exception.status_code, 400)

    def test_another_spawns_scratch_is_refused(self):
        """La riscrittura non deve allargare l'isolamento fra spawn."""
        other = self.spawn_root.parent / "messaggero-1" / "scratch"
        other.mkdir(parents=True)
        with self.assertRaises(HTTPException):
            transfers._scratch_path(self.spawn, str(other / "x.zip"))

    def test_an_empty_dest_is_refused_with_a_usable_message(self):
        with self.assertRaises(HTTPException) as cm:
            transfers._scratch_path(self.spawn, "   ")
        self.assertIn(str(self.scratch), cm.exception.detail)


if __name__ == "__main__":
    unittest.main()
