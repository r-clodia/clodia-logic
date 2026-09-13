"""`setup_pending` si riaccendeva da solo, e `setup_done` non lo sapeva.

clodia-platform#347: `packs.setup_done(base-pack)` conferma `setup_pending:
false`; qualche minuto dopo `packs.show(base-pack)` lo ritrova `true`, senza
mutazioni nel mezzo. Tre difetti, tutti nel punto in cui il marker si scrive:

1. la rotta `setup-done` rispondeva `{"setup_pending": False}` COSTANTE, mentre
   l'`unlink` viveva in un `except OSError: pass`: la conferma non era una
   misura e la segnalazione non era falsificabile;
2. i due scrittori del marker (post-import e update) accendevano in modo
   INCONDIZIONATO — l'update ri-marcava un pack già convergente, l'import
   marcava anche i pack del batch che non dichiarano niente;
3. il marker era un file VUOTO: acceso il flag, nessuno poteva dire da chi né
   perché, e i log del caso originale erano già usciti dalla retention.

I test qui sotto sono la cosa più piccola che diventa rossa se uno dei tre
difetti torna.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from . import packs

SOLO_SKILL = {"name": "solo-skill"}
CON_DATASTORE = {"name": "con-datastore", "datastores": [{"name": "contacts"}]}
CON_DATASTORE_E_RAG = {**CON_DATASTORE,
                       "rag_collections": [{"name": "normativa"}]}


def _richiesta() -> Request:
    return Request({"type": "http", "method": "POST",
                    "path": "/clodia/packs/x/setup-done", "headers": []})


class _Istanza:
    """Un DATA/packs finto con i pack indicati, e la lista plugin sostituita."""

    def __init__(self, packs_installati: dict[str, list[dict]]):
        self._packs = packs_installati
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        plugin_items: list[dict] = []
        for nome, plugins in packs_installati.items():
            d = self.root / nome
            d.mkdir(parents=True, exist_ok=True)
            (d / "pack.yaml").write_text(
                "name: %s\nplugins:\n%s\n" % (
                    nome, "\n".join(f"  - {p['name']}" for p in plugins) or "  []"),
                encoding="utf-8")
            plugin_items += plugins
        self._patches = [
            patch.object(packs.pack_import, "PACKS_META_DIR", self.root),
            patch.object(packs.plugins_api, "list_plugins",
                         lambda: [dict(p) for p in plugin_items]),
        ]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()
        return False


class SetupDoneDiceLaVeritaTests(unittest.TestCase):

    def test_non_risponde_fatto_se_il_marker_non_e_stato_rimosso(self):
        """Difetto 1: `unlink` fallisce, la rotta rispondeva comunque «fatto»."""
        with _Istanza({"studio-legale": [CON_DATASTORE]}):
            packs._setup_marker_path("studio-legale").write_text("", encoding="utf-8")

            async def _authz(_req, _tool):
                return "sysadmin"

            with patch.object(packs.gateway_pdp, "require_authz_async", _authz), \
                 patch.object(Path, "unlink", side_effect=OSError("read-only fs")):
                out = asyncio.run(packs.mark_pack_setup_done("studio-legale",
                                                            _richiesta()))
        # Prima: `{"setup_pending": False}` con il marker ancora sul disco.
        self.assertEqual(getattr(out, "status_code", 200), 500)

    def test_lo_stato_ritornato_e_riletto_dal_disco(self):
        with _Istanza({"studio-legale": [CON_DATASTORE]}):
            self.assertTrue(packs.set_setup_pending("studio-legale", True,
                                                    reason="import", by="test"))
            self.assertFalse(packs.set_setup_pending("studio-legale", False))


class LaGuardiaEuSimmetricaTests(unittest.TestCase):

    def test_non_marca_un_pack_che_non_dichiara_nulla_da_provisionare(self):
        """Difetto 2a: l'import marcava OGNI pack del batch, anche i vicini."""
        with _Istanza({"solo-skill-pack": [SOLO_SKILL],
                       "con-setup": [CON_DATASTORE]}):
            self.assertFalse(packs.set_setup_pending("solo-skill-pack", True,
                                                     reason="import", by="test"))
            self.assertFalse(packs._setup_marker_path("solo-skill-pack").is_file())
            self.assertTrue(packs.set_setup_pending("con-setup", True,
                                                    reason="import", by="test"))

    def test_un_update_senza_dichiarazioni_nuove_non_riaccende_il_marker(self):
        """Difetto 2b: il caso letterale della #347 su `base-pack`."""
        with _Istanza({"base-pack": [CON_DATASTORE]}):
            packs.set_setup_pending("base-pack", True, reason="import", by="test")
            packs.set_setup_pending("base-pack", False)
            packs.record_setup_done("base-pack", by="sysadmin")

            riacceso = packs.set_setup_pending("base-pack", True,
                                               reason="update da r-clodia/clodia-packs@main",
                                               by="packs.update")
        self.assertFalse(riacceso)

    def test_un_update_che_porta_dichiarazioni_nuove_lo_riaccende(self):
        """La guardia non deve spegnere il segnale quando serve davvero."""
        with _Istanza({"base-pack": [CON_DATASTORE]}) as ist:
            packs.record_setup_done("base-pack", by="sysadmin")
            with patch.object(packs.plugins_api, "list_plugins",
                              lambda: [dict(CON_DATASTORE_E_RAG)]):
                (ist.root / "base-pack" / "pack.yaml").write_text(
                    "name: base-pack\nplugins:\n  - con-datastore\n", encoding="utf-8")
                riacceso = packs.set_setup_pending("base-pack", True,
                                                   reason="update", by="packs.update")
        self.assertTrue(riacceso)


class IlMarkerDiceChiEPercheTests(unittest.TestCase):

    def test_show_espone_setup_pending_reason(self):
        """Difetto 3: acceso il flag, la causa non era leggibile da nessuna parte."""
        with _Istanza({"studio-legale": [CON_DATASTORE]}):
            packs.set_setup_pending("studio-legale", True,
                                    reason="update da r-clodia/clodia-packs@main",
                                    by="packs.update")
            pack = asyncio.run(packs.get_pack("studio-legale"))
        self.assertTrue(pack["setup_pending"])
        self.assertIn("update da r-clodia/clodia-packs@main",
                      pack["setup_pending_reason"])
        self.assertIn("packs.update", pack["setup_pending_reason"])

    def test_senza_marker_la_causa_e_vuota(self):
        with _Istanza({"studio-legale": [CON_DATASTORE]}):
            pack = asyncio.run(packs.get_pack("studio-legale"))
        self.assertFalse(pack["setup_pending"])
        self.assertEqual(pack["setup_pending_reason"], "")

    def test_un_marker_vuoto_pre_347_non_e_un_errore(self):
        with _Istanza({"studio-legale": [CON_DATASTORE]}):
            packs._setup_marker_path("studio-legale").write_text("", encoding="utf-8")
            self.assertEqual(packs.setup_pending_reason("studio-legale"), "")
            pack = asyncio.run(packs.get_pack("studio-legale"))
        self.assertTrue(pack["setup_pending"])


if __name__ == "__main__":
    unittest.main()
