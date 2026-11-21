"""clodia-platform#567 — a pack cannot overwrite another pack's plugin.

Plugin names are not namespaced per pack: before this, a third-party pack
shipping `plugins/<name of another pack's plugin>` rewrote that plugin's
manifest and `mcp/` code at import or Update, and a mounted MCP server ran the
new code at its next restart.

A plugin belongs to the pack that installed it (the installed pack whose
manifest lists it). Measured here:

1. who owns a plugin;
2. another pack's import of that plugin is refused BEFORE anything is written;
3. the owner itself may reinstall it, and so may a first-party Update
   (`trusted`, `allow_reserved`);
4. a bare plugin import (the wrapper path) obeys the same rule;
5. a pack Update passes `trusted` only for a first-party repository.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from . import pack_import, plugin_import


def _installed(base: Path, pack: str, plugins: list[str]) -> None:
    d = base / "packs" / pack
    d.mkdir(parents=True, exist_ok=True)
    (d / "pack.yaml").write_text(yaml.safe_dump({"name": pack, "plugins": plugins}),
                                 encoding="utf-8")
    for p in plugins:
        (base / "plugins" / p).mkdir(parents=True, exist_ok=True)


def _source_pack(base: Path, pack: str, plugin: str) -> Path:
    root = base / "src" / pack
    (root / "plugins" / plugin / "skills" / "s").mkdir(parents=True, exist_ok=True)
    (root / "plugins" / plugin / "skills" / "s" / "SKILL.md").write_text(
        "---\nname: s\ndescription: d\n---\nx\n", encoding="utf-8")
    (root / "pack.yaml").write_text(yaml.safe_dump({"name": pack, "version": "1.0.0"}),
                                    encoding="utf-8")
    return root


class _Env(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        (self.base / "packs").mkdir()
        (self.base / "plugins").mkdir()
        self.installed: list[str] = []

        def fake_install(root, **kw):
            self.installed.append(kw.get("default_name") or root.name)
            return {"plugin": kw.get("default_name") or root.name, "skills": [],
                    "rules": [], "mcp_servers": []}

        for p in (patch.object(pack_import, "PACKS_META_DIR", self.base / "packs"),
                  patch.object(plugin_import, "PLUGINS_META_DIR", self.base / "plugins"),
                  patch.object(plugin_import, "install_plugin_from_root",
                               side_effect=fake_install),
                  patch.object(pack_import, "validate_connectors", return_value={}),
                  patch.object(pack_import.registry, "load")):
            p.start()
            self.addCleanup(p.stop)
        _installed(self.base, "good-pack", ["victim"])


class OwnershipTests(_Env):
    def test_the_owner_is_the_pack_listing_it(self) -> None:
        self.assertEqual(pack_import.plugin_owners("victim"), ["good-pack"])
        self.assertEqual(pack_import.plugin_owners("victim", except_pack="good-pack"), [])
        self.assertEqual(pack_import.plugin_owners("nobody"), [])

    def test_another_pack_is_refused_before_writing(self) -> None:
        root = _source_pack(self.base, "evil-pack", "victim")
        with self.assertRaises(pack_import.PackImportError) as e:
            pack_import.install_pack_from_root(root, source="zip")
        self.assertIn("appartiene al pack 'good-pack'", str(e.exception))
        self.assertEqual(self.installed, [])
        self.assertFalse((self.base / "packs" / "evil-pack").exists())

    def test_a_forced_third_party_update_is_refused_too(self) -> None:
        root = _source_pack(self.base, "evil-pack", "victim")
        with self.assertRaises(pack_import.PackImportError):
            pack_import.install_pack_from_root(root, source="github:acme/x",
                                               force=True, force_only=set())
        self.assertEqual(self.installed, [])

    def test_the_owner_may_reinstall_it(self) -> None:
        root = _source_pack(self.base, "good-pack", "victim")
        pack_import.install_pack_from_root(root, source="zip")
        self.assertEqual(self.installed, ["victim"])

    def test_a_first_party_update_may_replace_it(self) -> None:
        for kw in ({"trusted": True}, {"allow_reserved": True}):
            with self.subTest(**kw):
                self.installed.clear()
                root = _source_pack(self.base, "evil-pack", "victim")
                pack_import.install_pack_from_root(root, source="github:r-clodia/x", **kw)
                self.assertEqual(self.installed, ["victim"])

    def test_a_new_plugin_name_is_not_affected(self) -> None:
        root = _source_pack(self.base, "evil-pack", "fresh")
        pack_import.install_pack_from_root(root, source="zip")
        self.assertEqual(self.installed, ["fresh"])

    def test_a_bare_plugin_import_obeys_the_same_rule(self) -> None:
        root = self.base / "bare"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text('{"name": "victim"}',
                                                              encoding="utf-8")
        with self.assertRaises(pack_import.PackImportError):
            pack_import.install_pack_from_root(root, source="zip")
        self.assertEqual(self.installed, [])


class UpdateTrustTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_passes_trusted_only_for_first_party(self) -> None:
        from unittest.mock import AsyncMock

        from . import packs
        calls: list[dict] = []

        def download(up, td):
            d = Path(td) / "src"
            (d / "plugins" / "acme-pack").mkdir(parents=True)
            (d / "pack.yaml").write_text(yaml.safe_dump({"name": "acme-pack"}),
                                         encoding="utf-8")
            return d

        def install(root, **kw):
            calls.append(kw)
            return {"kind": "pack", "pack": "acme-pack"}

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(packs, "_download_upstream_tarball", side_effect=download),
                  patch.object(packs.pack_import, "install_pack_from_root", side_effect=install),
                  patch.object(packs.pack_mcp_mount, "auto_mount_imported_mcp"),
                  patch.object(packs.plugins_api, "invalidate_plugins"),
                  patch.object(packs.registry, "load"),
                  patch.object(packs, "set_setup_pending"),
                  patch.object(packs, "_refresh_gateway_components", new=AsyncMock()),
                  patch.object(packs.pack_state, "is_disabled", return_value=False),
                  patch.object(pack_import, "PACKS_META_DIR", Path(tmp.name))):
            p.start()
            self.addCleanup(p.stop)
        for repo, expected in (("r-clodia/clodia-packs", True), ("acme/packs", False)):
            calls.clear()
            with patch.object(packs, "_pack_upstream",
                              return_value={"repo": repo, "path": "x", "ref": "main"}):
                try:
                    await packs._perform_update("acme-pack", "davide")
                except Exception:  # noqa: BLE001 — only the install call is measured
                    pass
            with self.subTest(repo=repo):
                self.assertTrue(calls, "install_pack_from_root not reached")
                self.assertIs(calls[-1].get("trusted"), expected)


if __name__ == "__main__":
    unittest.main()
