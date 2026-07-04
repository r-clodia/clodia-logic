"""Test dell'entità Pack: enumerazione, import (Claude plugin / pack.yaml /
bare skills), masking dei secret MCP, delete."""
from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml

from . import catalog, pack_import, packs


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _skill_md(name: str, description: str = "una skill") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n"


class PacksApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.logic_skills = root / "logic-skills"
        self.data_skills = root / "data-skills"
        self.logic_rules = root / "logic-rules"
        self.data_rules = root / "data-rules"
        self.packs_meta = root / "packs-meta"
        for p in (self.logic_skills, self.data_skills, self.logic_rules,
                  self.data_rules, self.packs_meta):
            p.mkdir()

        self._old_catalog = (
            catalog.LOGIC_SKILLS_DIR, catalog.DATA_SKILLS_DIR,
            catalog.LOGIC_RULES_DIR, catalog.DATA_RULES_DIR,
        )
        catalog.LOGIC_SKILLS_DIR = self.logic_skills
        catalog.DATA_SKILLS_DIR = self.data_skills
        catalog.LOGIC_RULES_DIR = self.logic_rules
        catalog.DATA_RULES_DIR = self.data_rules
        self._old_meta = pack_import.PACKS_META_DIR
        pack_import.PACKS_META_DIR = self.packs_meta
        self._old_manifest = packs.EXTERNAL_PACKS_MANIFEST
        packs.EXTERNAL_PACKS_MANIFEST = root / "external-packs.yaml"
        self._clear_caches()

    def tearDown(self) -> None:
        (
            catalog.LOGIC_SKILLS_DIR, catalog.DATA_SKILLS_DIR,
            catalog.LOGIC_RULES_DIR, catalog.DATA_RULES_DIR,
        ) = self._old_catalog
        pack_import.PACKS_META_DIR = self._old_meta
        packs.EXTERNAL_PACKS_MANIFEST = self._old_manifest
        self._clear_caches()
        self.tmp.cleanup()

    def _clear_caches(self) -> None:
        for cache in catalog._LIST_CACHE.values():
            cache["ts"] = 0.0
            cache["data"] = None
        for cache in catalog._DETAIL_CACHE.values():
            cache.clear()
        packs._invalidate_packs()

    def _skill(self, root: Path, *parts: str) -> None:
        d = root.joinpath(*parts)
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(_skill_md(parts[-1]), encoding="utf-8")

    def _rule(self, root: Path, *parts: str) -> None:
        f = root.joinpath(*parts)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# Rule\nuna rule\n", encoding="utf-8")

    def _by_name(self, name: str) -> dict:
        for p in packs._list_packs():
            if p["name"] == name:
                return p
        raise AssertionError(f"pack '{name}' non trovato")

    # --- enumerazione -----------------------------------------------------

    def test_list_base_local_and_subdir_packs(self) -> None:
        self._skill(self.logic_skills, "fact-check")
        self._rule(self.logic_rules, "git-style.md")
        self._skill(self.data_skills, "my-flat-skill")           # → local-pack
        self._skill(self.data_skills, "acme-pack", "pdf")        # → acme-pack
        self._rule(self.data_rules, "acme-pack", "blog-voice.md")
        self._rule(self.data_rules, "loose-rule.md")             # → local-pack

        names = [p["name"] for p in packs._list_packs()]
        self.assertEqual(names[0], "base-pack")  # base-pack sempre in testa
        self.assertEqual(set(names), {"base-pack", "local-pack", "acme-pack"})

        base = self._by_name("base-pack")
        self.assertEqual([s["name"] for s in base["skills"]], ["fact-check"])
        self.assertEqual([r["name"] for r in base["rules"]], ["git-style"])
        self.assertEqual(base["origin"], "logic")
        self.assertFalse(base["deletable"])

        acme = self._by_name("acme-pack")
        self.assertEqual([s["name"] for s in acme["skills"]], ["pdf"])
        self.assertEqual([r["name"] for r in acme["rules"]], ["blog-voice"])
        self.assertEqual(acme["origin"], "imported")
        self.assertTrue(acme["deletable"])
        self.assertEqual(acme["counts"], {"skills": 1, "rules": 1, "mcp_servers": 0})

        local = self._by_name("local-pack")
        self.assertEqual([s["name"] for s in local["skills"]], ["my-flat-skill"])
        self.assertFalse(local["deletable"])

    def test_external_origin_from_manifest(self) -> None:
        self._skill(self.data_skills, "anthropic-pack", "pdf")
        packs.EXTERNAL_PACKS_MANIFEST.write_text(
            "- pack: anthropic-pack\n  repo: https://example.com/x\n  ref: main\n  subdir: skills\n",
            encoding="utf-8",
        )
        item = self._by_name("anthropic-pack")
        self.assertEqual(item["origin"], "external")
        self.assertTrue(item["deletable"])

    def test_mcp_only_pack_and_secret_masking(self) -> None:
        (self.packs_meta / "mcp-only").mkdir()
        (self.packs_meta / "mcp-only" / "pack.yaml").write_text(yaml.safe_dump({
            "name": "mcp-only",
            "description": "Solo MCP",
            "mcp_servers": {
                "weather": {
                    "type": "http",
                    "url": "https://mcp.example.com/",
                    "headers": {"Authorization": "Bearer s3cret",
                                "X-Api-Key": "${WEATHER_KEY}"},
                },
            },
        }), encoding="utf-8")
        item = self._by_name("mcp-only")
        self.assertEqual(item["counts"], {"skills": 0, "rules": 0, "mcp_servers": 1})
        srv = item["mcp_servers"][0]
        self.assertEqual(srv["name"], "weather")
        self.assertEqual(srv["transport"], "http")
        headers = srv["config"]["headers"]
        self.assertEqual(headers["Authorization"], "•••")          # secret mascherato
        self.assertEqual(headers["X-Api-Key"], "${WEATHER_KEY}")   # placeholder visibile

    # --- import -----------------------------------------------------------

    def test_import_claude_plugin_zip(self) -> None:
        data = _zip_bytes({
            "my-plugin/.claude-plugin/plugin.json":
                '{"name": "My Plugin", "description": "demo", "version": "1.2.0"}',
            "my-plugin/skills/hello/SKILL.md": _skill_md("hello"),
            "my-plugin/.mcp.json":
                '{"mcpServers": {"srv": {"command": "npx", "args": ["x"]}}}',
        })
        result = pack_import.import_pack_zip(data, source="my-plugin.zip")
        self.assertEqual(result["pack"], "my-plugin")  # nome sanificato
        self.assertEqual(result["skills"], ["hello"])
        self.assertEqual(result["mcp_servers"], ["srv"])
        self.assertTrue(
            (self.data_skills / "my-plugin" / "hello" / "SKILL.md").is_file())
        manifest = yaml.safe_load(
            (self.packs_meta / "my-plugin" / "pack.yaml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "1.2.0")
        self.assertIn("srv", manifest["mcp_servers"])
        self._clear_caches()
        item = self._by_name("my-plugin")
        self.assertEqual(item["origin"], "imported")
        self.assertEqual(item["mcp_servers"][0]["transport"], "stdio")

    def test_import_pack_yaml_zip_with_rules(self) -> None:
        data = _zip_bytes({
            "pack.yaml": yaml.safe_dump({
                "name": "acme-pack", "description": "Pack ACME",
                "mcp_servers": {"kb": {"type": "http", "url": "https://kb/"}},
            }),
            "skills/pdf/SKILL.md": _skill_md("pdf"),
            "rules/blog-voice.md": "# Rule\nvoce del blog\n",
        })
        result = pack_import.import_pack_zip(data)
        self.assertEqual(result["pack"], "acme-pack")
        self.assertEqual(result["skills"], ["pdf"])
        self.assertEqual(result["rules"], ["blog-voice"])
        self.assertTrue((self.data_rules / "acme-pack" / "blog-voice.md").is_file())

    def test_import_bare_skills_falls_back_to_user_pack(self) -> None:
        data = _zip_bytes({"hello/SKILL.md": _skill_md("hello")})
        result = pack_import.import_pack_zip(data)
        self.assertEqual(result["pack"], "user-pack")
        self.assertEqual(result["skills"], ["hello"])
        self.assertTrue(
            (self.data_skills / "user-pack" / "hello" / "SKILL.md").is_file())

    def test_import_reserved_pack_name_rejected(self) -> None:
        data = _zip_bytes({
            "pack.yaml": "name: base-pack\n",
            "skills/x/SKILL.md": _skill_md("x"),
        })
        with self.assertRaises(pack_import.PackImportError):
            pack_import.import_pack_zip(data)

    def test_import_empty_pack_rejected(self) -> None:
        data = _zip_bytes({"pack.yaml": "name: empty-pack\n"})
        with self.assertRaises(pack_import.PackImportError):
            pack_import.import_pack_zip(data)

    # --- delete -----------------------------------------------------------

    def test_delete_pack_removes_all_components(self) -> None:
        self._skill(self.data_skills, "acme-pack", "pdf")
        self._rule(self.data_rules, "acme-pack", "style.md")
        (self.packs_meta / "acme-pack").mkdir()
        (self.packs_meta / "acme-pack" / "pack.yaml").write_text(
            "name: acme-pack\n", encoding="utf-8")

        res = asyncio.run(packs.delete_pack("acme-pack"))
        self.assertEqual(res, {"deleted": "acme-pack"})
        self.assertFalse((self.data_skills / "acme-pack").exists())
        self.assertFalse((self.data_rules / "acme-pack").exists())
        self.assertFalse((self.packs_meta / "acme-pack").exists())

    def test_delete_reserved_pack_forbidden(self) -> None:
        res = asyncio.run(packs.delete_pack("base-pack"))
        self.assertEqual(res.status_code, 403)

    def test_delete_missing_pack_404(self) -> None:
        res = asyncio.run(packs.delete_pack("ghost-pack"))
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
