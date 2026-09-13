"""clodia-platform#347 · `setup_done` non può dire «fatto» senza aver guardato.

Il sintomo riportato: `packs.setup_done(base-pack)` risponde
`setup_pending: false`, e pochi minuti dopo `packs.show(base-pack)` torna a dire
`setup_pending: true` senza che nessuno abbia importato o aggiornato niente.

L'issue ipotizzava un job che re-flagga i pack con `has_upstream`. Nel codice
quel job non esiste: il marker `DATA/packs/<n>/.setup_pending` ha **tre soli
scrittori**, tutti dietro una rotta HTTP (import, update, setup-done). Quello che
esisteva davvero è una risposta che non era una lettura:

    set_setup_pending(name, False)          # `except OSError: pass`
    return {"name": name, "setup_pending": False}   # costante, mai verificata

Se l'`unlink` falliva — meta dir scritta da un altro uid, filesystem in sola
lettura, permessi — il marker restava, l'errore non usciva da nessuna parte, e
l'unico modo di accorgersene era vedere il flag ricomparire al `packs.show`
successivo. Da fuori è indistinguibile da un job fantasma, ed è esattamente la
diagnosi che l'issue ha dovuto indovinare.

Nota sul pack della segnalazione: su `base-pack` `needs_setup` è `true` **per
costruzione** e non è un drift — il suo plugin dichiara il datastore `contacts`
(`catalogs/packs/base-pack/pack.yaml`), e `_pack_needs_setup` guarda le
dichiarazioni dei plugin, non i `mcp_servers: []` del manifest. L'unica variabile
è il marker: per questo i test qui sotto sono tutti sul marker.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import packs


class _Base(unittest.TestCase):
    """Una meta dir vera in un tmpdir: il marker è un file, e questa è l'unica
    cosa che serve sapere per riprodurre la #347."""

    PACK = "base-pack"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.meta = Path(self.tmp.name) / "packs"
        (self.meta / self.PACK).mkdir(parents=True)
        self._patch = patch.object(packs.pack_import, "PACKS_META_DIR", self.meta)
        self._patch.start()
        self.marker = self.meta / self.PACK / ".setup_pending"
        self.marker.write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self._patch.stop()
        self.tmp.cleanup()

    def _setup_done(self):
        async def _authz(_req, _tool):
            return "sysadmin"

        with patch.object(packs.gateway_pdp, "require_authz_async", _authz):
            return asyncio.run(packs.mark_pack_setup_done(self.PACK, None))


class TheAnswerIsAReadingNotAPromiseTests(_Base):

    def test_a_failed_unlink_is_not_reported_as_success(self) -> None:
        """Il difetto della #347, riprodotto: l'`unlink` non riesce e la rotta
        rispondeva comunque `setup_pending: false`. Rosso prima del fix."""
        def _boom(_self):
            raise PermissionError(13, "Permission denied")

        with patch.object(Path, "unlink", _boom):
            res = self._setup_done()

        self.assertTrue(self.marker.is_file(), "premessa: il marker è rimasto lì")
        self.assertEqual(500, getattr(res, "status_code", 200),
                         "una rimozione fallita non è un successo")
        # Il valore riportato è quello che `packs.show` dirà, non un desiderio.
        self.assertEqual(packs._observed_setup_pending(self.PACK),
                         json.loads(res.body)["setup_pending"])

    def test_the_two_routes_agree_after_a_failure(self) -> None:
        """La sostanza della segnalazione: due letture consecutive che si
        contraddicono. Dopo il fix non è più possibile — la risposta di
        `setup-done` esce dalla stessa lettura di `packs.show`."""
        def _boom(_self):
            raise OSError(30, "Read-only file system")

        with patch.object(Path, "unlink", _boom):
            res = self._setup_done()

        detto = json.loads(res.body)["setup_pending"]
        self.assertEqual(packs._observed_setup_pending(self.PACK), detto)

    def test_a_real_removal_still_answers_false(self) -> None:
        """Controprova: il caso che funziona deve continuare a funzionare, e la
        risposta resta la stessa di prima per chi la consuma."""
        res = self._setup_done()

        self.assertFalse(self.marker.exists())
        self.assertEqual({"name": self.PACK, "setup_pending": False}, res)


class TheMarkerSaysWhatItDidTests(_Base):

    def test_set_setup_pending_returns_the_observed_state(self) -> None:
        """Il valore di ritorno è il marker DOPO l'operazione, non quello
        richiesto: è il dato che mancava al chiamante. Rosso prima (la funzione
        ritornava `None` in ogni caso)."""
        self.assertFalse(packs.set_setup_pending(self.PACK, False))
        self.assertTrue(packs.set_setup_pending(self.PACK, True))

        def _boom(_self):
            raise PermissionError(13, "Permission denied")

        with patch.object(Path, "unlink", _boom):
            self.assertTrue(packs.set_setup_pending(self.PACK, False),
                            "unlink fallito: il marker è ancora pendente")

    def test_a_swallowed_error_leaves_a_line_in_the_log(self) -> None:
        """Senza questa riga una ricomparsa del flag non ha un colpevole: gli
        scrittori del marker sono tre e nessuno lasciava traccia."""
        def _boom(_self):
            raise PermissionError(13, "Permission denied")

        with patch.object(Path, "unlink", _boom), \
             self.assertLogs(packs.LOG, level="WARNING") as log:
            packs.set_setup_pending(self.PACK, False)

        self.assertTrue(any("Permission denied" in r for r in log.output),
                        f"il motivo non compare nel log: {log.output}")


if __name__ == "__main__":
    unittest.main()
