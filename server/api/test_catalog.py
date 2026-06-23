from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from . import catalog


class CatalogApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.logic_skills = root / "logic-skills"
        self.data_skills = root / "data-skills"
        self.logic_rules = root / "logic-rules"
        self.data_rules = root / "data-rules"
        for path in (self.logic_skills, self.data_skills, self.logic_rules, self.data_rules):
            path.mkdir()

        self.old_paths = (
            catalog.LOGIC_SKILLS_DIR,
            catalog.DATA_SKILLS_DIR,
            catalog.LOGIC_RULES_DIR,
            catalog.DATA_RULES_DIR,
        )
        catalog.LOGIC_SKILLS_DIR = self.logic_skills
        catalog.DATA_SKILLS_DIR = self.data_skills
        catalog.LOGIC_RULES_DIR = self.logic_rules
        catalog.DATA_RULES_DIR = self.data_rules
        self._clear_cache()

    def tearDown(self) -> None:
        (
            catalog.LOGIC_SKILLS_DIR,
            catalog.DATA_SKILLS_DIR,
            catalog.LOGIC_RULES_DIR,
            catalog.DATA_RULES_DIR,
        ) = self.old_paths
        self._clear_cache()
        self.tmp.cleanup()

    def _clear_cache(self) -> None:
        for cache in catalog._LIST_CACHE.values():
            cache["ts"] = 0.0
            cache["data"] = None
        for cache in catalog._DETAIL_CACHE.values():
            cache.clear()

    def _skill(
        self,
        root: Path,
        name: str,
        description: str,
        *,
        pack: str | None = None,
    ) -> None:
        path = root / name
        path.mkdir()
        pack_line = f"pack: {pack}\n" if pack else ""
        (path / "SKILL.md").write_text(
            f"---\nname: {name}\n{pack_line}description: |\n  {description}\n  second line\n---\n# Skill: {name}\n",
            encoding="utf-8",
        )

    def test_skills_dedup_and_data_override(self) -> None:
        self._skill(self.logic_skills, "feature-spec", "Logic feature spec")
        self._skill(self.logic_skills, "code-review", "Logic review")
        self._skill(self.data_skills, "code-review", "Data review wins")

        rows = catalog._list_catalog("skill")

        self.assertEqual([r["name"] for r in rows], ["code-review", "feature-spec"])
        code_review = rows[0]
        self.assertEqual(code_review["source"], "both")
        self.assertEqual(code_review["pack"], "local-pack")
        self.assertEqual(code_review["available_packs"], ["base-pack", "local-pack"])
        self.assertEqual(
            [(v["pack"], v["active"]) for v in code_review["variants"]],
            [("base-pack", False), ("local-pack", True)],
        )
        self.assertEqual(code_review["available_in"], ["logic", "data"])
        self.assertIn("data-skills/code-review/SKILL.md", code_review["path"])
        self.assertEqual(code_review["description"], "Data review wins")

        detail = catalog._resolve_detail("skill", "code-review")
        self.assertEqual(detail["source"], "both")
        self.assertEqual(detail["pack"], "local-pack")
        self.assertIn("# Skill: code-review", detail["body"])
        self.assertEqual(detail["frontmatter"]["name"], "code-review")

    def test_data_pack_inference_and_explicit_pack(self) -> None:
        self._skill(self.logic_skills, "feature-spec", "Logic feature spec")
        self._skill(self.data_skills, "blog-writing", "Data-only skill")
        self._skill(
            self.data_skills,
            "custom-export",
            "Installed from a custom pack",
            pack="finance-pack",
        )

        rows = {r["name"]: r for r in catalog._list_catalog("skill")}

        self.assertEqual(rows["feature-spec"]["pack"], "base-pack")
        # data-only senza pack esplicito → local-pack (nessun special-case di brand)
        self.assertEqual(rows["blog-writing"]["pack"], "local-pack")
        self.assertEqual(rows["custom-export"]["pack"], "finance-pack")

    def _pack_skill(self, root: Path, pack: str, name: str, description: str) -> None:
        """Crea una skill in un pack-subdir: root/<pack>/<name>/SKILL.md."""
        self._skill(root / pack, name, description)

    def test_pack_subdir_label_from_path(self) -> None:
        # pack-subdir: il pack viene dal PATH, non dal frontmatter.
        (self.data_skills / "anthropic-pack").mkdir()
        self._pack_skill(self.data_skills, "anthropic-pack", "mcp-builder", "Build MCP")
        rows = {r["name"]: r for r in catalog._list_catalog("skill")}
        self.assertIn("mcp-builder", rows)
        self.assertEqual(rows["mcp-builder"]["pack"], "anthropic-pack")
        self.assertEqual(rows["mcp-builder"]["source"], "data")

    def test_same_name_in_two_packs_no_collision(self) -> None:
        # `pdf` esiste in due pack → una sola riga, due varianti, niente collisione.
        (self.data_skills / "anthropic-pack").mkdir()
        (self.data_skills / "openai-curated-pack").mkdir()
        self._pack_skill(self.data_skills, "anthropic-pack", "pdf", "Anthropic pdf")
        self._pack_skill(self.data_skills, "openai-curated-pack", "pdf", "OpenAI pdf")
        rows = {r["name"]: r for r in catalog._list_catalog("skill")}
        self.assertIn("pdf", rows)
        self.assertEqual(
            sorted(rows["pdf"]["available_packs"]),
            ["anthropic-pack", "openai-curated-pack"],
        )
        # esattamente una variante attiva
        self.assertEqual(sum(1 for v in rows["pdf"]["variants"] if v["active"]), 1)

    def test_rules_skip_readme_and_describe_from_body(self) -> None:
        (self.logic_rules / "README.md").write_text("# Catalog docs\n", encoding="utf-8")
        (self.logic_rules / "python-style.md").write_text(
            "---\nglobs:\n  - '**/*.py'\n---\n# Rule: Python Style\n\nUse type hints.\n",
            encoding="utf-8",
        )
        (self.data_rules / "python-style.md").write_text(
            "---\nglobs:\n  - '**/*.py'\n---\n# Rule: Python Style\n\nUse stricter type hints.\n",
            encoding="utf-8",
        )

        rows = catalog._list_catalog("rule")

        self.assertEqual([r["name"] for r in rows], ["python-style"])
        self.assertEqual(rows[0]["source"], "both")
        self.assertEqual(rows[0]["pack"], "local-pack")
        self.assertEqual(rows[0]["description"], "Use stricter type hints.")

    def test_invalid_and_missing_names(self) -> None:
        with self.assertRaises(HTTPException) as invalid:
            catalog._resolve_detail("skill", "../passwd")
        self.assertEqual(invalid.exception.status_code, 400)

        with self.assertRaises(HTTPException) as missing:
            catalog._resolve_detail("skill", "missing")
        self.assertEqual(missing.exception.status_code, 404)

    def _make_zip(self, arc: dict[str, str]) -> bytes:
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, content in arc.items():
                z.writestr(name, content)
        return buf.getvalue()

    def test_import_zip_lands_in_user_pack(self) -> None:
        from . import skill_import
        data = self._make_zip({
            "my-skill/SKILL.md": "---\nname: my-skill\ndescription: imported\n---\n# x\n",
            "my-skill/scripts/run.py": "print('hi')\n",
        })
        names = skill_import.import_zip(data)
        self.assertEqual(names, ["my-skill"])
        # finita nel pack-subdir user-pack, con asset
        dst = self.data_skills / catalog.USER_PACK / "my-skill"
        self.assertTrue((dst / "SKILL.md").is_file())
        self.assertTrue((dst / "scripts" / "run.py").is_file())
        # visibile in catalogo col pack user-pack
        rows = {r["name"]: r for r in catalog._list_catalog("skill")}
        self.assertEqual(rows["my-skill"]["pack"], catalog.USER_PACK)

    def test_import_zip_rejects_zip_slip(self) -> None:
        from . import skill_import
        data = self._make_zip({"../evil/SKILL.md": "---\nname: evil\n---\n"})
        with self.assertRaises(skill_import.SkillImportError):
            skill_import.import_zip(data)

    def test_import_zip_rejects_native_name(self) -> None:
        from . import skill_import
        self._skill(self.logic_skills, "fact-check", "native")
        data = self._make_zip({"fact-check/SKILL.md": "---\nname: fact-check\n---\n"})
        with self.assertRaises(skill_import.SkillImportError):
            skill_import.import_zip(data)

    def test_delete_user_pack_skill(self) -> None:
        from . import skill_import
        skill_import.import_zip(self._make_zip(
            {"tmp/SKILL.md": "---\nname: removable\ndescription: d\n---\n# x\n"}))
        d = catalog._require_user_skill_dir("removable")
        self.assertTrue(d.is_dir())
        # una skill nativa non è rimovibile
        self._skill(self.logic_skills, "article-spec", "native")
        with self.assertRaises(HTTPException) as e:
            catalog._require_user_skill_dir("article-spec")
        self.assertEqual(e.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
