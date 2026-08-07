"""Una riga d'indice che punta a un topic inesistente non deve bloccare quello vero.

Successo il 7 ago 2026. `proof-of-flex-2` era passato da SEAL-2 a SEAL-1; il
registro degli hook tiene coppie `(tier, name)` e nessuno l'ha aggiornato al
cambio di tier. Creare l'hook del topic vero rispondeva:

    HTTP 409 — slug 'proof-of-flex-2' già usato da SEAL-2/proof-of-flex-2

citando un topic che sul disco non c'era. Lo stesso schema ricorso più volte in
questi due giorni: un registro esiste, la cosa sotto si sposta, e nessuno tiene
insieme le due.

Si ripara al momento del conflitto invece che con una migrazione: il
disallineamento può ripresentarsi a ogni cambio di tier, e un controllo che si
auto-guarisce non ha bisogno che qualcuno se ne ricordi.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import db


class Base(unittest.TestCase):
    def setUp(self):
        import tempfile
        import pathlib
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hooks-"))
        self.p = patch.object(db, "_FILE", self.tmp / "hooks.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class GhostRowTests(Base):
    def test_a_row_pointing_at_a_missing_topic_is_cleared(self):
        """Il caso di Davide, per intero."""
        db.create("SEAL-2", "proof-of-flex-2", "vecchio", "davide")
        with patch.object(db, "_topic_exists", lambda t, n: t == "SEAL-1"):
            row, _secret = db.create("SEAL-1", "proof-of-flex-2", "nuovo", "davide")
        self.assertEqual(row["tier"], "SEAL-1")
        righe = [(r["tier"], r["name"]) for r in db.list_all()] \
            if hasattr(db, "list_all") else [(r["tier"], r["name"]) for r in db._load()]
        self.assertNotIn(("SEAL-2", "proof-of-flex-2"), righe)

    def test_a_row_pointing_at_a_LIVE_topic_still_conflicts(self):
        """L'unicità globale dello slug resta: due topic veri con lo stesso nome
        in tier diversi sono ancora un errore, perché lo slug è anche l'id del
        webhook e deve identificarne uno solo."""
        db.create("SEAL-2", "duplicato", "a", "davide")
        with patch.object(db, "_topic_exists", lambda t, n: True):
            with self.assertRaises(db.HookConflictError):
                db.create("SEAL-1", "duplicato", "b", "davide")

    def test_an_unreachable_gateway_does_not_delete_a_real_hook(self):
        """La direzione d'errore che conta. Se il controllo di esistenza fallisce
        — gateway giù, riavvio in corso — deve rispondere «esiste» e mantenere il
        conflitto. Il contrario significherebbe cancellare hook veri ogni volta
        che qualcosa si riavvia."""
        db.create("SEAL-2", "vero", "a", "davide")
        with patch.object(db, "topics_client_open_or_raise", create=True):
            with patch("server.api.topics_client.open_topic",
                       side_effect=RuntimeError("gateway giù")):
                with self.assertRaises(db.HookConflictError):
                    db.create("SEAL-1", "vero", "b", "davide")

    def test_the_existence_check_fails_closed_by_construction(self):
        import inspect
        src = inspect.getsource(db._topic_exists)
        self.assertIn("return True", src.split("except")[-1],
                      "su errore _topic_exists deve rispondere True (fail closed)")


if __name__ == "__main__":
    unittest.main()
