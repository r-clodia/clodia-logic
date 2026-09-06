"""La rotta che risponde «questo seed non è più quello del pack» (#266).

Tre cose che la rotta deve fare, e che il confronto puro (`seed_drift`) non può
fare da sé:

1. **risolvere il riferimento** — dopo l'install il pack sorgente non esiste più
   sull'istanza: `install_pack_from_root` copia i seed in `DATA/agents/` e scrive
   un manifest che elenca solo i nomi. Il termine di paragone va ritrovato:
   catalogo bundled se c'è, upstream altrimenti;
2. **dire quando NON può rispondere** — un pack senza riferimento non è un pack
   pulito. È la stessa distinzione che `pack_ops.drift` fa su `mounted=None`:
   senza, l'assenza di una fonte si legge come assenza di divergenze, che è
   esattamente la bugia che questa issue esiste per togliere;
3. **chiedere il verbo che implementa** — `packs.drift`. Le rotte read-only
   sorelle chiedono `packs.import_url` come sinonimo di «admin-only»: il prestito
   che `clodia-tools#242`/`clodia-logic#365` hanno appena corretto per la #297.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from ..agents.loader import registry
from . import gateway_pdp, pack_import, packs


def _seed(dir_: Path, nome: str, **campi) -> None:
    d = dir_ / nome
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.yaml").write_text(
        yaml.safe_dump({"name": nome, **campi}, allow_unicode=True, sort_keys=False),
        encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="drift-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.packs_meta = self.root / "packs-meta"
        self.agents_dir = self.root / "agents"
        self.workspace = self.root / "workspace"
        for p in (self.packs_meta, self.agents_dir, self.workspace):
            p.mkdir(parents=True)

        self._old_meta = pack_import.PACKS_META_DIR
        pack_import.PACKS_META_DIR = self.packs_meta
        self.addCleanup(setattr, pack_import, "PACKS_META_DIR", self._old_meta)
        self._old_base = registry.base_dir
        registry.base_dir = self.agents_dir
        self.addCleanup(setattr, registry, "base_dir", self._old_base)

        # `_bundle_catalog_dir` risolve via `workspace_path`: si sostituisce
        # quella, non il resolver, così la rotta esercita il resolver vero.
        p = mock.patch.object(packs, "workspace_path",
                              lambda rel: self.workspace / rel)
        p.start()
        self.addCleanup(p.stop)
        packs._drift_cache_clear()

        self.verbi: list[str] = []
        p = mock.patch.object(
            gateway_pdp, "require_authz",
            lambda request, tool: self.verbi.append(tool) or "davide")
        p.start()
        self.addCleanup(p.stop)

    # -- helper ------------------------------------------------------------

    def installa_pack(self, nome: str, **manifest) -> None:
        d = self.packs_meta / nome
        d.mkdir(parents=True, exist_ok=True)
        (d / "pack.yaml").write_text(
            yaml.safe_dump({"name": nome, **manifest}, sort_keys=False),
            encoding="utf-8")

    def bundle_agents_dir(self, nome: str) -> Path:
        d = self.workspace / "catalogs" / "packs" / nome
        (d / "agents").mkdir(parents=True, exist_ok=True)
        (d / "pack.yaml").write_text(f"name: {nome}\nversion: 1.0.0\n", encoding="utf-8")
        return d / "agents"

    def drift(self, nome: str = "base-pack") -> dict:
        return asyncio.run(packs.check_pack_drift(nome, mock.Mock()))


class RiferimentoBundledTests(Base):
    def test_a_field_removed_from_the_installed_seed_is_reported(self):
        pack = self.bundle_agents_dir("base-pack")
        _seed(pack, "sysadmin", gated_tools=["packs.remove"])
        _seed(self.agents_dir, "sysadmin")
        self.installa_pack("base-pack", agents=["sysadmin"])

        res = self.drift()
        self.assertEqual(res["source"], "bundled")
        self.assertEqual(res["drifted"], 1)
        self.assertEqual(res["agents"][0]["name"], "sysadmin")
        self.assertEqual(res["agents"][0]["missing"], ["gated_tools"])

    def test_seeds_that_match_produce_no_drift(self):
        pack = self.bundle_agents_dir("base-pack")
        _seed(pack, "clodia", gated_tools=["a"])
        _seed(self.agents_dir, "clodia", gated_tools=["a"])
        self.installa_pack("base-pack", agents=["clodia"])

        res = self.drift()
        self.assertEqual(res["drifted"], 0)
        self.assertEqual(res["agents"], [])
        self.assertEqual(res["checked"], 1)


class SenzaRiferimentoTests(Base):
    def test_a_pack_without_any_reference_says_it_cannot_answer(self):
        """Né bundled né upstream: «non calcolabile», mai «nessuna divergenza»."""
        self.installa_pack("pack-di-terzi", agents=["qualcuno"])
        _seed(self.agents_dir, "qualcuno")

        res = self.drift("pack-di-terzi")
        self.assertTrue(res["unavailable"])
        self.assertTrue(res["reason"])
        self.assertNotIn("drifted", res)
        self.assertNotIn("agents", res)


class VerboTests(Base):
    def test_the_route_asks_for_the_verb_it_implements(self):
        """`packs.drift`, non `packs.import_url` preso in prestito da vicino: un
        rifiuto deve nominare l'azione vera (lezione della #297)."""
        self.bundle_agents_dir("base-pack")
        self.installa_pack("base-pack", agents=[])
        self.drift()
        self.assertEqual(self.verbi, ["packs.drift"])


class ListaTests(Base):
    def test_the_list_says_whether_the_question_can_be_answered(self):
        """Il bottone non deve promettere una risposta che non esiste: la lista
        dice per ogni pack se un riferimento c'è."""
        self.bundle_agents_dir("base-pack")
        self.installa_pack("base-pack", agents=[])
        self.installa_pack("pack-di-terzi", agents=[])
        per_nome = {p["name"]: p for p in packs._list_packs()}
        self.assertTrue(per_nome["base-pack"]["drift_checkable"])
        self.assertFalse(per_nome["pack-di-terzi"]["drift_checkable"])


class NomeTests(Base):
    def test_an_invalid_pack_name_is_refused(self):
        res = self.drift("../etc")
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
