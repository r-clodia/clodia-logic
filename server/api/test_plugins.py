"""Test dell'entità Plugin: enumerazione, import (Claude plugin / plugin.yaml /
bare skills), masking dei secret MCP, delete."""
from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml

from . import catalog, plugin_import, plugins


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _skill_md(name: str, description: str = "una skill") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n"


class PluginsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.logic_skills = root / "logic-skills"
        self.data_skills = root / "data-skills"
        self.logic_rules = root / "logic-rules"
        self.data_rules = root / "data-rules"
        self.plugins_meta = root / "plugins-meta"
        for p in (self.logic_skills, self.data_skills, self.logic_rules,
                  self.data_rules, self.plugins_meta):
            p.mkdir()

        self._old_catalog = (
            catalog.LOGIC_SKILLS_DIR, catalog.DATA_SKILLS_DIR,
            catalog.LOGIC_RULES_DIR, catalog.DATA_RULES_DIR,
        )
        catalog.LOGIC_SKILLS_DIR = self.logic_skills
        catalog.DATA_SKILLS_DIR = self.data_skills
        catalog.LOGIC_RULES_DIR = self.logic_rules
        catalog.DATA_RULES_DIR = self.data_rules
        self._old_meta = plugin_import.PLUGINS_META_DIR
        plugin_import.PLUGINS_META_DIR = self.plugins_meta
        self._old_manifest = plugins.EXTERNAL_PACKS_MANIFEST
        plugins.EXTERNAL_PACKS_MANIFEST = root / "external-packs.yaml"
        self._clear_caches()

    def tearDown(self) -> None:
        (
            catalog.LOGIC_SKILLS_DIR, catalog.DATA_SKILLS_DIR,
            catalog.LOGIC_RULES_DIR, catalog.DATA_RULES_DIR,
        ) = self._old_catalog
        plugin_import.PLUGINS_META_DIR = self._old_meta
        plugins.EXTERNAL_PACKS_MANIFEST = self._old_manifest
        self._clear_caches()
        self.tmp.cleanup()

    def _clear_caches(self) -> None:
        for cache in catalog._LIST_CACHE.values():
            cache["ts"] = 0.0
            cache["data"] = None
        for cache in catalog._DETAIL_CACHE.values():
            cache.clear()
        plugins.invalidate_plugins()

    def _skill(self, root: Path, *parts: str) -> None:
        d = root.joinpath(*parts)
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(_skill_md(parts[-1]), encoding="utf-8")

    def _rule(self, root: Path, *parts: str) -> None:
        f = root.joinpath(*parts)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# Rule\nuna rule\n", encoding="utf-8")

    def _by_name(self, name: str) -> dict:
        for p in plugins.list_plugins():
            if p["name"] == name:
                return p
        raise AssertionError(f"plugin '{name}' non trovato")

    # --- enumerazione -----------------------------------------------------

    def test_list_base_local_and_subdir_plugins(self) -> None:
        self._skill(self.logic_skills, "fact-check")
        self._rule(self.logic_rules, "git-style.md")
        self._skill(self.data_skills, "my-flat-skill")           # → local-pack
        self._skill(self.data_skills, "acme-pack", "pdf")        # → acme-pack
        self._rule(self.data_rules, "acme-pack", "blog-voice.md")
        self._rule(self.data_rules, "loose-rule.md")             # → local-pack

        names = [p["name"] for p in plugins.list_plugins()]
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

    def test_external_origin_from_manifest(self) -> None:
        self._skill(self.data_skills, "anthropic-pack", "pdf")
        plugins.EXTERNAL_PACKS_MANIFEST.write_text(
            "- pack: anthropic-pack\n  repo: https://example.com/x\n  ref: main\n  subdir: skills\n",
            encoding="utf-8",
        )
        item = self._by_name("anthropic-pack")
        self.assertEqual(item["origin"], "external")
        self.assertTrue(item["deletable"])

    def test_mcp_only_plugin_and_secret_masking(self) -> None:
        (self.plugins_meta / "mcp-only").mkdir()
        (self.plugins_meta / "mcp-only" / "plugin.yaml").write_text(yaml.safe_dump({
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
        result = plugin_import.import_plugin_zip(data, source="my-plugin.zip")
        self.assertEqual(result["plugin"], "my-plugin")  # nome sanificato
        self.assertEqual(result["skills"], ["hello"])
        self.assertEqual(result["mcp_servers"], ["srv"])
        self.assertTrue(
            (self.data_skills / "my-plugin" / "hello" / "SKILL.md").is_file())
        manifest = yaml.safe_load(
            (self.plugins_meta / "my-plugin" / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "1.2.0")
        self.assertIn("srv", manifest["mcp_servers"])
        self._clear_caches()
        item = self._by_name("my-plugin")
        self.assertEqual(item["origin"], "imported")
        self.assertEqual(item["mcp_servers"][0]["transport"], "stdio")

    def test_import_plugin_yaml_zip_with_rules(self) -> None:
        data = _zip_bytes({
            "plugin.yaml": yaml.safe_dump({
                "name": "acme-pack", "description": "Plugin ACME",
                "mcp_servers": {"kb": {"type": "http", "url": "https://kb/"}},
            }),
            "skills/pdf/SKILL.md": _skill_md("pdf"),
            "rules/blog-voice.md": "# Rule\nvoce del blog\n",
        })
        result = plugin_import.import_plugin_zip(data)
        self.assertEqual(result["plugin"], "acme-pack")
        self.assertEqual(result["skills"], ["pdf"])
        self.assertEqual(result["rules"], ["blog-voice"])
        self.assertTrue((self.data_rules / "acme-pack" / "blog-voice.md").is_file())

    def test_import_legacy_pack_yaml_still_works(self) -> None:
        # pack.yaml SENZA agents/plugins = manifest di plugin legacy (v6.57)
        data = _zip_bytes({
            "pack.yaml": "name: legacy-pack\ndescription: vecchio formato\n",
            "skills/x/SKILL.md": _skill_md("x"),
        })
        result = plugin_import.import_plugin_zip(data)
        self.assertEqual(result["plugin"], "legacy-pack")
        self.assertEqual(result["skills"], ["x"])

    def test_import_bare_skills_falls_back_to_user_pack(self) -> None:
        data = _zip_bytes({"hello/SKILL.md": _skill_md("hello")})
        result = plugin_import.import_plugin_zip(data)
        self.assertEqual(result["plugin"], "user-pack")
        self.assertEqual(result["skills"], ["hello"])
        self.assertTrue(
            (self.data_skills / "user-pack" / "hello" / "SKILL.md").is_file())

    def test_default_name_installs_bare_dir_as_named_plugin(self) -> None:
        # Directory plugin dentro un pack, senza manifest proprio: il nome
        # viene dal path (default_name), non da user-pack.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "skills" / "hello").mkdir(parents=True)
            (root / "skills" / "hello" / "SKILL.md").write_text(
                _skill_md("hello"), encoding="utf-8")
            result = plugin_import.install_plugin_from_root(
                root, source="test", default_name="inner-plugin")
        self.assertEqual(result["plugin"], "inner-plugin")
        self.assertTrue(
            (self.data_skills / "inner-plugin" / "hello" / "SKILL.md").is_file())

    def test_import_reserved_plugin_name_rejected(self) -> None:
        data = _zip_bytes({
            "plugin.yaml": "name: base-pack\n",
            "skills/x/SKILL.md": _skill_md("x"),
        })
        with self.assertRaises(plugin_import.PluginImportError):
            plugin_import.import_plugin_zip(data)

    def test_import_empty_plugin_rejected(self) -> None:
        data = _zip_bytes({"plugin.yaml": "name: empty-plugin\n"})
        with self.assertRaises(plugin_import.PluginImportError):
            plugin_import.import_plugin_zip(data)

    # --- delete -----------------------------------------------------------

    def test_delete_plugin_removes_all_components(self) -> None:
        self._skill(self.data_skills, "acme-pack", "pdf")
        self._rule(self.data_rules, "acme-pack", "style.md")
        (self.plugins_meta / "acme-pack").mkdir()
        (self.plugins_meta / "acme-pack" / "plugin.yaml").write_text(
            "name: acme-pack\n", encoding="utf-8")

        res = asyncio.run(plugins.delete_plugin("acme-pack"))
        self.assertEqual(res, {"deleted": "acme-pack"})
        self.assertFalse((self.data_skills / "acme-pack").exists())
        self.assertFalse((self.data_rules / "acme-pack").exists())
        self.assertFalse((self.plugins_meta / "acme-pack").exists())

    def test_delete_reserved_plugin_forbidden(self) -> None:
        res = asyncio.run(plugins.delete_plugin("base-pack"))
        self.assertEqual(res.status_code, 403)

    def test_delete_missing_plugin_404(self) -> None:
        res = asyncio.run(plugins.delete_plugin("ghost-plugin"))
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
