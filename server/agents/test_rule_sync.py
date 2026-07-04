"""Test della risoluzione rule pack-aware (speculare a test_skill_sync).

Rule bare (`python-style`) o qualificata (`acme-pack/python-style`); data flat,
data pack-subdir, logic. Pack-glob `<pack>/*` e wildcard `*`.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from . import rule_sync as R


def _rule(root: Path, *parts: str) -> Path:
    f = root.joinpath(*parts)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("---\nglobs: ['**/*.py']\n---\n# Rule\nbody\n", encoding="utf-8")
    return f


class RuleSyncPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.logic = root / "logic"
        self.data = root / "data"
        self.target = root / "target"
        self.logic.mkdir()
        self.data.mkdir()
        self._old = (R.LOGIC_CATALOG_DIR, R.DATA_CATALOG_DIR)
        R.LOGIC_CATALOG_DIR = self.logic
        R.DATA_CATALOG_DIR = self.data

    def tearDown(self) -> None:
        R.LOGIC_CATALOG_DIR, R.DATA_CATALOG_DIR = self._old
        self.tmp.cleanup()

    def test_bare_resolves_logic(self) -> None:
        _rule(self.logic, "git-style.md")
        self.assertEqual(R._resolve_rule_source("git-style"), self.logic / "git-style.md")

    def test_bare_resolves_pack_subdir(self) -> None:
        _rule(self.data, "acme-pack", "blog-voice.md")
        self.assertEqual(
            R._resolve_rule_source("blog-voice"),
            self.data / "acme-pack" / "blog-voice.md",
        )

    def test_qualified_resolves_exact_pack(self) -> None:
        _rule(self.data, "acme-pack", "style.md")
        _rule(self.data, "other-pack", "style.md")
        self.assertEqual(
            R._resolve_rule_source("other-pack/style"),
            self.data / "other-pack" / "style.md",
        )

    def test_data_flat_precedes_pack_and_logic(self) -> None:
        _rule(self.logic, "style.md")
        _rule(self.data, "acme-pack", "style.md")
        _rule(self.data, "style.md")
        self.assertEqual(R._resolve_rule_source("style"), self.data / "style.md")

    def test_all_names_dedup(self) -> None:
        _rule(self.logic, "a.md")
        _rule(self.data, "a.md")
        _rule(self.data, "acme-pack", "b.md")
        (self.data / "README.md").write_text("doc", encoding="utf-8")
        self.assertEqual(R._all_rule_names(), ["a", "b"])

    def test_pack_glob_and_materialize(self) -> None:
        _rule(self.data, "acme-pack", "one.md")
        _rule(self.data, "acme-pack", "two.md")
        copied, unresolved = R.materialize_rules(["acme-pack/*"], self.target)
        self.assertEqual(copied, 2)
        self.assertEqual(unresolved, [])
        # rule qualificate → nome file de-collisionato <pack>__<rule>.md
        self.assertTrue((self.target / "acme-pack__one.md").is_file())
        self.assertTrue((self.target / "acme-pack__two.md").is_file())

    def test_wildcard_materializes_all(self) -> None:
        _rule(self.logic, "a.md")
        _rule(self.data, "acme-pack", "b.md")
        copied, unresolved = R.materialize_rules(["*"], self.target)
        self.assertEqual(copied, 2)
        self.assertEqual(unresolved, [])
        self.assertTrue((self.target / "a.md").is_file())
        self.assertTrue((self.target / "b.md").is_file())


if __name__ == "__main__":
    unittest.main()
