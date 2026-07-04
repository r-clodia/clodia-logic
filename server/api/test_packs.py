"""Test dell'entità Pack: pack = [agent seeds] + [plugins].

Import unificato (pack vs plugin sciolto), install+registrazione dei seed,
requires_plugins soft (missing → warning, mai errore), delete."""
from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml

from ..agents.loader import registry
from . import catalog, pack_import, packs, plugin_import, plugins


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: una skill\n---\n# {name}\n"


def _agent_yaml(name: str, requires: list | None = None) -> str:
    spec = {
        "name": name,
        "display_name": name.capitalize(),
        "description": f"Agente di test {name}",
        "type": "normal",
        "system_prompt": "system-prompt.md",
        "capabilities": [],
        "requires_plugins": requires or [],
    }
    return yaml.safe_dump(spec, sort_keys=False)


class PacksApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.logic_skills = root / "logic-skills"
        self.data_skills = root / "data-skills"
        self.logic_rules = root / "logic-rules"
        self.data_rules = root / "data-rules"
        self.plugins_meta = root / "plugins-meta"
        self.packs_meta = root / "packs-meta"
        self.agents_dir = root / "agents"
        for p in (self.logic_skills, self.data_skills, self.logic_rules,
                  self.data_rules, self.plugins_meta, self.packs_meta,
                  self.agents_dir):
            p.mkdir()

        self._old_catalog = (
            catalog.LOGIC_SKILLS_DIR, catalog.DATA_SKILLS_DIR,
            catalog.LOGIC_RULES_DIR, catalog.DATA_RULES_DIR,
        )
        catalog.LOGIC_SKILLS_DIR = self.logic_skills
        catalog.DATA_SKILLS_DIR = self.data_skills
        catalog.LOGIC_RULES_DIR = self.logic_rules
        catalog.DATA_RULES_DIR = self.data_rules
        self._old_plugins_meta = plugin_import.PLUGINS_META_DIR
        plugin_import.PLUGINS_META_DIR = self.plugins_meta
        self._old_packs_meta = pack_import.PACKS_META_DIR
        pack_import.PACKS_META_DIR = self.packs_meta
        self._old_manifest = plugins.EXTERNAL_PACKS_MANIFEST
        plugins.EXTERNAL_PACKS_MANIFEST = root / "external-packs.yaml"
        self._old_agents_dir = registry.base_dir
        registry.base_dir = self.agents_dir
        registry.load()
        self._clear_caches()

    def tearDown(self) -> None:
        (
            catalog.LOGIC_SKILLS_DIR, catalog.DATA_SKILLS_DIR,
            catalog.LOGIC_RULES_DIR, catalog.DATA_RULES_DIR,
        ) = self._old_catalog
        plugin_import.PLUGINS_META_DIR = self._old_plugins_meta
        pack_import.PACKS_META_DIR = self._old_packs_meta
        plugins.EXTERNAL_PACKS_MANIFEST = self._old_manifest
        registry.base_dir = self._old_agents_dir
        registry.load()
        self._clear_caches()
        self.tmp.cleanup()

    def _clear_caches(self) -> None:
        for cache in catalog._LIST_CACHE.values():
            cache["ts"] = 0.0
            cache["data"] = None
        for cache in catalog._DETAIL_CACHE.values():
            cache.clear()
        plugins.invalidate_plugins()

    def _pack_zip(self) -> bytes:
        """Pack completo: 1 seed (con requires soft) + 1 plugin con manifest
        proprio + 1 plugin bare (nome dal path)."""
        return _zip_bytes({
            "my-pack/pack.yaml": "name: my-pack\ndescription: Pack di test\nversion: 1.0.0\n",
            "my-pack/agents/testbot/agent.yaml": _agent_yaml(
                "testbot",
                requires=[{"name": "inner-plugin", "hard": False},
                          {"name": "not-installed", "hard": False}],
            ),
            "my-pack/agents/testbot/system-prompt.md": "# Testbot\n",
            "my-pack/plugins/inner-plugin/.claude-plugin/plugin.json":
                '{"name": "inner-plugin", "description": "demo", "version": "0.1.0"}',
            "my-pack/plugins/inner-plugin/skills/hello/SKILL.md": _skill_md("hello"),
            "my-pack/plugins/bare-plugin/skills/world/SKILL.md": _skill_md("world"),
        })

    # --- import -----------------------------------------------------------

    def test_import_pack_installs_agents_and_plugins(self) -> None:
        result = pack_import.import_pack_zip(self._pack_zip(), source="my-pack.zip")
        self.assertEqual(result["kind"], "pack")
        self.assertEqual(result["pack"], "my-pack")
        self.assertEqual(result["agents"], [{"name": "testbot", "status": "installed"}])
        self.assertEqual({p["plugin"] for p in result["plugins"]},
                         {"inner-plugin", "bare-plugin"})
        # seed installato e registrato nel registry
        self.assertTrue((self.agents_dir / "testbot" / "agent.yaml").is_file())
        self.assertTrue((self.agents_dir / "testbot" / "memory").is_dir())
        self.assertIsNotNone(registry.get_by_name("testbot"))
        # plugin sul filesystem
        self.assertTrue(
            (self.data_skills / "inner-plugin" / "hello" / "SKILL.md").is_file())
        self.assertTrue(
            (self.data_skills / "bare-plugin" / "world" / "SKILL.md").is_file())
        # manifest del pack
        manifest = yaml.safe_load(
            (self.packs_meta / "my-pack" / "pack.yaml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["agents"], ["testbot"])
        self.assertEqual(sorted(manifest["plugins"]), ["bare-plugin", "inner-plugin"])

    def test_list_packs_exposes_soft_missing_plugins(self) -> None:
        pack_import.import_pack_zip(self._pack_zip(), source="my-pack.zip")
        self._clear_caches()
        items = packs._list_packs()
        self.assertEqual(len(items), 1)
        pack = items[0]
        self.assertEqual(pack["name"], "my-pack")
        agent = pack["agents"][0]
        self.assertEqual(agent["name"], "testbot")
        self.assertTrue(agent["installed"])
        # requires soft: inner-plugin installato, not-installed → warning
        self.assertEqual(agent["missing_plugins"], ["not-installed"])
        plugin_names = {p["name"] for p in pack["plugins"]}
        self.assertEqual(plugin_names, {"inner-plugin", "bare-plugin"})
        self.assertEqual(pack["counts"], {"agents": 1, "plugins": 2})

    def test_unified_import_falls_back_to_plugin(self) -> None:
        data = _zip_bytes({
            ".claude-plugin/plugin.json": '{"name": "loose-plugin"}',
            "skills/solo/SKILL.md": _skill_md("solo"),
        })
        result = pack_import.import_pack_zip(data)
        self.assertEqual(result["kind"], "plugin")
        self.assertEqual(result["plugin"], "loose-plugin")

    # --- marketplace (repo Claude multi-plugin) ----------------------------

    def _marketplace_zip(self, plugins_entry: list | None = None) -> bytes:
        """Repo marketplace stile clodia-plugins, incapsulato come gli zip
        GitHub (`repo-main/`): marketplace.json + 2 plugin + 1 seed."""
        manifest = {
            "name": "clodia-plugins",
            "owner": {"name": "r-clodia"},
            "plugins": plugins_entry if plugins_entry is not None else [
                {"name": "studio-commercialista",
                 "source": "./plugins/studio-commercialista",
                 "description": "skill commercialista + MCP normattiva"},
            ],
        }
        return _zip_bytes({
            "repo-main/.claude-plugin/marketplace.json": json.dumps(manifest),
            "repo-main/plugins/studio-commercialista/.claude-plugin/plugin.json":
                '{"name": "studio-commercialista", "description": "demo", '
                '"version": "0.1.0"}',
            "repo-main/plugins/studio-commercialista/.mcp.json":
                '{"mcpServers": {"normattiva": {"command": "python3", '
                '"args": ["mcp/normattiva_mcp.py"]}}}',
            "repo-main/plugins/studio-commercialista/skills/consulenza-normativa/SKILL.md":
                _skill_md("consulenza-normativa"),
            # plugin presente nel repo ma NON dichiarato nel marketplace
            "repo-main/plugins/non-dichiarato/.claude-plugin/plugin.json":
                '{"name": "non-dichiarato"}',
            "repo-main/plugins/non-dichiarato/skills/extra/SKILL.md": _skill_md("extra"),
            # seed (estensione Clodia, fuori dallo standard marketplace)
            "repo-main/seeds/mktbot/agent.yaml": _agent_yaml("mktbot"),
            "repo-main/seeds/mktbot/system-prompt.md": "# Mktbot\n",
        })

    def test_import_marketplace_repo_as_pack(self) -> None:
        result = pack_import.import_pack_zip(
            self._marketplace_zip(), source="https://github.com/r-clodia/clodia-plugins")
        self.assertEqual(result["kind"], "pack")
        self.assertEqual(result["pack"], "clodia-plugins")
        self.assertEqual({p["plugin"] for p in result["plugins"]},
                         {"studio-commercialista"})  # solo i dichiarati
        self.assertEqual(result["agents"], [{"name": "mktbot", "status": "installed"}])
        # skill nel pack-subdir del plugin, non in user-pack
        self.assertTrue((self.data_skills / "studio-commercialista" /
                         "consulenza-normativa" / "SKILL.md").is_file())
        self.assertFalse((self.data_skills / catalog.USER_PACK).exists())
        # MCP server del plugin nel manifest (esposto, non montato)
        pmanifest = yaml.safe_load(
            (self.plugins_meta / "studio-commercialista" / "plugin.yaml")
            .read_text(encoding="utf-8"))
        self.assertIn("normattiva", pmanifest["mcp_servers"])
        # manifest del pack
        manifest = yaml.safe_load(
            (self.packs_meta / "clodia-plugins" / "pack.yaml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["plugins"], ["studio-commercialista"])
        self.assertEqual(manifest["agents"], ["mktbot"])

    def test_marketplace_missing_source_errors(self) -> None:
        data = self._marketplace_zip(plugins_entry=[
            {"name": "fantasma", "source": "./plugins/inesistente"},
        ])
        with self.assertRaises(pack_import.PackImportError):
            pack_import.import_pack_zip(data)

    def test_marketplace_unsafe_source_errors(self) -> None:
        data = self._marketplace_zip(plugins_entry=[
            {"name": "evil", "source": "../../fuori"},
        ])
        with self.assertRaises(pack_import.PackImportError):
            pack_import.import_pack_zip(data)

    def test_seed_native_name_rejected_seed_existing_skipped(self) -> None:
        (self.agents_dir / "existing").mkdir()
        (self.agents_dir / "existing" / "agent.yaml").write_text(
            _agent_yaml("existing"), encoding="utf-8")
        registry.load()
        data = _zip_bytes({
            "pack.yaml": "name: seeds-pack\n",
            "agents/clodia/agent.yaml": _agent_yaml("clodia"),
            "agents/existing/agent.yaml": _agent_yaml("existing"),
            "agents/fresh/agent.yaml": _agent_yaml("fresh"),
            "agents/fresh/system-prompt.md": "# Fresh\n",
        })
        result = pack_import.import_pack_zip(data)
        by_name = {a["name"]: a["status"] for a in result["agents"]}
        self.assertEqual(by_name["clodia"], "error")     # nativo
        self.assertEqual(by_name["existing"], "exists")  # non sovrascritto
        self.assertEqual(by_name["fresh"], "installed")
        # il manifest non elenca gli errori
        manifest = yaml.safe_load(
            (self.packs_meta / "seeds-pack" / "pack.yaml").read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest["agents"]), ["existing", "fresh"])

    def test_invalid_seed_rolled_back(self) -> None:
        data = _zip_bytes({
            "pack.yaml": "name: bad-pack\n",
            "agents/badbot/agent.yaml": "name: badbot\nunknown_field: boom\n",
        })
        with self.assertRaises(pack_import.PackImportError):
            # unico componente e non valido → import fallisce
            pack_import.import_pack_zip(data)
        self.assertFalse((self.agents_dir / "badbot").exists())
        self.assertIsNone(registry.get_by_name("badbot"))

    # --- delete -----------------------------------------------------------

    def test_delete_pack_removes_plugins_and_agents(self) -> None:
        pack_import.import_pack_zip(self._pack_zip(), source="my-pack.zip")
        self._clear_caches()
        res = asyncio.run(packs.delete_pack("my-pack"))
        self.assertEqual(res["deleted"], "my-pack")
        self.assertEqual(sorted(res["plugins"]), ["bare-plugin", "inner-plugin"])
        self.assertEqual(res["agents"], ["testbot"])
        self.assertFalse((self.agents_dir / "testbot").exists())
        self.assertFalse((self.data_skills / "inner-plugin").exists())
        self.assertFalse((self.packs_meta / "my-pack").exists())
        self.assertIsNone(registry.get_by_name("testbot"))

    def test_delete_missing_pack_404(self) -> None:
        res = asyncio.run(packs.delete_pack("ghost-pack"))
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
