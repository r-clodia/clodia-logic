"""Test della risoluzione capability pack-aware (21 giu 2026).

Capability bare (`pdf`) o qualificata (`anthropic-pack/pdf`); data flat, data
pack-subdir, logic. Materializzazione con nome runtime de-collisionato.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from . import skill_sync as S


def _skill(root: Path, *parts: str) -> Path:
    d = root.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("---\nname: x\ndescription: d\n---\n# x\n", encoding="utf-8")
    return d


class SkillSyncPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.logic = root / "logic"
        self.data = root / "data"
        self.logic.mkdir()
        self.data.mkdir()
        self._old = (S.LOGIC_CATALOG_DIR, S.DATA_CATALOG_DIR)
        S.LOGIC_CATALOG_DIR = self.logic
        S.DATA_CATALOG_DIR = self.data

    def tearDown(self) -> None:
        S.LOGIC_CATALOG_DIR, S.DATA_CATALOG_DIR = self._old
        self.tmp.cleanup()

    def test_bare_resolves_logic(self) -> None:
        _skill(self.logic, "article-spec")
        self.assertEqual(S._resolve_skill_source("article-spec"), self.logic / "article-spec")

    def test_bare_resolves_pack_subdir(self) -> None:
        _skill(self.data, "anthropic-pack", "mcp-builder")
        self.assertEqual(
            S._resolve_skill_source("mcp-builder"),
            self.data / "anthropic-pack" / "mcp-builder",
        )

    def test_qualified_resolves_exact_pack(self) -> None:
        _skill(self.data, "anthropic-pack", "pdf")
        _skill(self.data, "openai-curated-pack", "pdf")
        self.assertEqual(
            S._resolve_skill_source("openai-curated-pack/pdf"),
            self.data / "openai-curated-pack" / "pdf",
        )

    def test_data_flat_precedes_logic(self) -> None:
        _skill(self.logic, "fact-check")
        _skill(self.data, "fact-check")
        self.assertEqual(S._resolve_skill_source("fact-check"), self.data / "fact-check")

    def test_all_names_dedup_across_packs(self) -> None:
        _skill(self.logic, "article-spec")
        _skill(self.data, "anthropic-pack", "pdf")
        _skill(self.data, "openai-curated-pack", "pdf")
        names = S._all_skill_names()
        self.assertEqual(names.count("pdf"), 1)  # dedup first-wins
        self.assertIn("article-spec", names)

    def test_materialize_qualified_decollides(self) -> None:
        _skill(self.data, "anthropic-pack", "pdf")
        _skill(self.data, "openai-curated-pack", "pdf")
        with tempfile.TemporaryDirectory() as out:
            target = Path(out)
            copied, unresolved = S.materialize_capabilities(
                ["anthropic-pack/pdf", "openai-curated-pack/pdf"], target)
            self.assertEqual(copied, 2)
            self.assertEqual(unresolved, [])
            self.assertTrue((target / "anthropic-pack__pdf" / "SKILL.md").is_file())
            self.assertTrue((target / "openai-curated-pack__pdf" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
