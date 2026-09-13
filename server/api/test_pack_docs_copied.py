"""SETUP.md/CHANGELOG.md a livello radice del pack arrivano nel datadir (#339).

`install_pack_from_root` scriveva solo un `pack.yaml` curato coi campi noti
(name/description/version/source/agents/plugins): un file come SETUP.md — il
runbook che Sysadmin legge per il protocollo di setup — restava sul sorgente e
non arrivava MAI nel `meta_dir` installato, né a un fresh install né a un
Update. Non era quindi il sospetto iniziale (un pack non re-importato dopo un
merge upstream): nessun re-import lo avrebbe portato, perché il file non era
proprio nel contratto di copia.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from . import catalog, pack_import, plugin_import


def _plugin(dir_: Path, nome: str) -> None:
    pdir = dir_ / "plugins" / nome
    d = pdir / ".claude-plugin"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(
        json.dumps({"name": nome, "description": "plugin di prova",
                    "version": "1.0.0"}),
        encoding="utf-8")
    skill = pdir / "skills" / "dummy"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\nname: dummy\ndescription: skill di prova\n---\n# dummy\n",
        encoding="utf-8")


class PackDocsCopiedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="packdocs-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.packs_meta = self.root / "packs-meta"
        self.packs_meta.mkdir(parents=True)
        self._old_meta = pack_import.PACKS_META_DIR
        pack_import.PACKS_META_DIR = self.packs_meta
        self.addCleanup(setattr, pack_import, "PACKS_META_DIR", self._old_meta)

        # `install_plugin_from_root` (chiamato a cascata) scrive plugin+skill
        # nei path DATA reali se non isolati: qui, non nel cwd del repo.
        self.plugins_meta = self.root / "plugins-meta"
        self.skills_meta = self.root / "skills-meta"
        self._old_plugins = plugin_import.PLUGINS_META_DIR
        plugin_import.PLUGINS_META_DIR = self.plugins_meta
        self.addCleanup(setattr, plugin_import, "PLUGINS_META_DIR", self._old_plugins)
        self._old_skills = catalog.DATA_SKILLS_DIR
        catalog.DATA_SKILLS_DIR = self.skills_meta
        self.addCleanup(setattr, catalog, "DATA_SKILLS_DIR", self._old_skills)

    def _pack_root(self, nome: str) -> Path:
        d = self.root / "src" / nome
        d.mkdir(parents=True)
        (d / "pack.yaml").write_text(
            yaml.safe_dump({"name": nome, "version": "1.0.0"}, sort_keys=False),
            encoding="utf-8")
        _plugin(d, "plugin-di-prova")
        return d

    def test_setup_md_arriva_nel_meta_dir(self):
        d = self._pack_root("con-doc")
        (d / "SETUP.md").write_text("# runbook\n", encoding="utf-8")
        pack_import.install_pack_from_root(d, source="test")
        installato = self.packs_meta / "con-doc" / "SETUP.md"
        self.assertTrue(installato.is_file())
        self.assertEqual(installato.read_text(encoding="utf-8"), "# runbook\n")

    def test_changelog_md_arriva_anch_esso(self):
        d = self._pack_root("con-changelog")
        (d / "CHANGELOG.md").write_text("# changelog\n", encoding="utf-8")
        pack_import.install_pack_from_root(d, source="test")
        self.assertTrue((self.packs_meta / "con-changelog" / "CHANGELOG.md").is_file())

    def test_un_pack_senza_doc_non_fallisce(self):
        """base-pack/bandi-pack prima del fix: setup banale, nessun file da
        copiare — deve restare un no-op silenzioso, non un errore."""
        d = self._pack_root("senza-doc")
        pack_import.install_pack_from_root(d, source="test")
        meta = self.packs_meta / "senza-doc"
        self.assertTrue((meta / "pack.yaml").is_file())
        self.assertFalse((meta / "SETUP.md").exists())

    def test_un_update_successivo_sovrascrive_il_doc(self):
        """Idempotenza: un Update che porta un SETUP.md aggiornato deve
        rimpiazzare quello vecchio, non lasciarlo stantio accanto al nuovo."""
        d = self._pack_root("aggiornato")
        (d / "SETUP.md").write_text("v1\n", encoding="utf-8")
        pack_import.install_pack_from_root(d, source="test")
        (d / "SETUP.md").write_text("v2\n", encoding="utf-8")
        pack_import.install_pack_from_root(d, source="test", force=True)
        self.assertEqual(
            (self.packs_meta / "aggiornato" / "SETUP.md").read_text(encoding="utf-8"),
            "v2\n")


if __name__ == "__main__":
    unittest.main()
