"""Risoluzione cross-pack del token `<DATASTORE:key>` (10 set 2026).

`contacts` può vivere nel manifest di un pack diverso da quello della skill
che lo referenzia (es. base-pack dichiara `contacts`, la skill `osint-lead`
resta nel pack `tomato`) — questi test coprono quella risoluzione.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from . import skill_sync as S


def _manifest(root: Path, pack: str, datastores: list[dict]) -> None:
    d = root / "plugins" / pack
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.yaml").write_text(
        yaml.safe_dump({"name": pack, "datastores": datastores}), encoding="utf-8")


class DatastoreMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._patch = patch.object(S, "data_path", side_effect=lambda rel: self.root / rel)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self.tmp.cleanup()

    def test_own_pack_resolves_as_before(self) -> None:
        _manifest(self.root, "tomato", [{"path": "data/leads.db"}])
        out = S._datastore_map("tomato")
        self.assertIn("leads", out)
        self.assertTrue(out["leads"].endswith("data/leads.db"))

    def test_resolves_a_datastore_declared_in_another_pack(self) -> None:
        """Il caso reale: `contacts` vive nel base-pack, la skill è in tomato."""
        _manifest(self.root, "tomato", [{"path": "data/leads.db"}])
        _manifest(self.root, "base-pack", [{"path": "data/contacts.db", "name": "contacts",
                                            "seeds": ["messaggero"]}])
        out = S._datastore_map("tomato")
        self.assertIn("leads", out)
        self.assertIn("contacts", out)
        self.assertTrue(out["contacts"].endswith("base-pack/data/contacts.db"))

    def test_own_pack_wins_on_collision(self) -> None:
        _manifest(self.root, "tomato", [{"path": "data/contacts.db"}])
        _manifest(self.root, "other-pack", [{"path": "elsewhere/contacts.db"}])
        out = S._datastore_map("tomato")
        self.assertTrue(out["contacts"].endswith("tomato/data/contacts.db"))

    def test_ambiguous_key_among_other_packs_keeps_first_alphabetically_and_logs(self) -> None:
        """Fra due pack DIVERSI da quello della skill, il primo in ordine
        alfabetico vince (deterministico) — il warning segnala l'ambiguità,
        non azzera la risoluzione."""
        _manifest(self.root, "pack-a", [{"path": "data/shared.db", "name": "shared"}])
        _manifest(self.root, "pack-b", [{"path": "elsewhere/shared.db", "name": "shared"}])
        with self.assertLogs(S.LOG.name, level="WARNING") as cm:
            out = S._datastore_map("tomato")
        self.assertTrue(out["shared"].endswith("pack-a/data/shared.db"))
        self.assertTrue(any("ambigua" in m for m in cm.output))

    def test_token_substitution_finds_a_cross_pack_datastore(self) -> None:
        _manifest(self.root, "base-pack", [{"path": "data/contacts.db", "name": "contacts"}])
        skill_dir = self.root / "skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "scrivi in <DATASTORE:contacts>\n", encoding="utf-8")
        S._substitute_datastore_tokens(skill_dir, "tomato")
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("<DATASTORE:", text)
        self.assertIn("base-pack/data/contacts.db", text)

    def test_unresolved_token_is_left_in_place(self) -> None:
        skill_dir = self.root / "skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "scrivi in <DATASTORE:nonexistent>\n", encoding="utf-8")
        S._substitute_datastore_tokens(skill_dir, "tomato")
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("<DATASTORE:nonexistent>", text)


if __name__ == "__main__":
    unittest.main()
