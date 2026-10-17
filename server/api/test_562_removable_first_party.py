"""clodia-platform#562 — only base-pack is non-removable.

One set, `RESERVED_PLUGIN_NAMES`, did three jobs: anti-spoofing on import,
first-party recognition, and non-removability. The third was meant for
base-pack alone, and kept comms-pack, google-pack, editorial-pack and
local-pack from ever being removed. Now `NON_REMOVABLE_PACK_NAMES` drives
removal; the reserved names keep the other two jobs.

Measured here:

1. base-pack (and its `logic` plugin) is still refused, by the endpoint and
   by `remove_pack`; a first-party pack is not;
2. a first-party pack's own plugin is removed with it (it was skipped);
3. the reserved names still refuse an import from zip/URL (anti-spoofing);
4. a removed bundled pack comes back only from `available` + `install`,
   which runs the trusted Update path — and nothing else is installable there;
5. a removed bundled pack's skills are not resolved from the image.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from ..agents import skill_sync
from . import gateway_pdp, pack_deprovision, pack_import, packs, plugin_import


class _Req:
    headers: dict = {}


def _write_pack(root: Path, name: str, **extra) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    m = {"name": name, "version": "2.0.0", "description": f"{name} pack",
         "upstream": {"repo": "r-clodia/clodia-logic",
                      "path": f"catalogs/packs/{name}", "ref": "main"}, **extra}
    (d / "pack.yaml").write_text(yaml.safe_dump(m), encoding="utf-8")


class _Env(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.workspace = base / "ws"
        self.data = base / "data"
        self.catalog = self.workspace / "catalogs" / "packs"
        self.installed = self.data / "packs"
        for n in ("base-pack", "comms-pack", "google-pack"):
            _write_pack(self.catalog, n)
        for n in ("base-pack", "comms-pack"):
            _write_pack(self.installed, n)
        for p in (
            patch.object(pack_import, "PACKS_META_DIR", self.installed),
            patch.object(packs, "workspace_path", side_effect=lambda rel: self.workspace / rel),
            patch.object(skill_sync, "data_path", side_effect=lambda rel: self.data / rel),
            patch.object(gateway_pdp, "require_authz_async", new=AsyncMock(return_value="davide")),
        ):
            p.start()
            self.addCleanup(p.stop)


class RemovalTests(_Env):
    def test_the_sets_are_distinct(self) -> None:
        self.assertEqual(plugin_import.NON_REMOVABLE_PACK_NAMES, {"base-pack", "logic"})
        for n in ("comms-pack", "google-pack", "editorial-pack", "local-pack"):
            self.assertIn(n, plugin_import.RESERVED_PLUGIN_NAMES)
            self.assertNotIn(n, plugin_import.NON_REMOVABLE_PACK_NAMES)

    async def test_base_pack_is_refused_by_the_endpoint(self) -> None:
        for n in ("base-pack", "logic"):
            r = await packs.delete_pack(n, _Req())
            self.assertEqual(r.status_code, 403, n)

    async def test_a_first_party_pack_is_removed(self) -> None:
        with patch.object(pack_deprovision, "snapshot_pack", return_value={}), \
             patch.object(pack_import, "remove_pack",
                          return_value={"deleted": "comms-pack", "plugins": ["comms-pack"],
                                        "agents": [], "datastores_archived": [],
                                        "datastores_retained": []}) as rm, \
             patch.object(pack_deprovision, "deprovision_async", new=AsyncMock(return_value=None)), \
             patch.object(packs.plugins_api, "invalidate_plugins"):
            out = await packs.delete_pack("comms-pack", _Req())
        rm.assert_called_once_with("comms-pack")
        self.assertEqual(out["deleted"], "comms-pack")

    def test_remove_pack_refuses_base_pack(self) -> None:
        with self.assertRaises(pack_import.PackImportError):
            pack_import.remove_pack("base-pack")

    def test_a_first_party_pack_takes_its_own_plugin_with_it(self) -> None:
        _write_pack(self.installed, "google-pack", plugins=["google-pack", "logic"])
        with patch.object(plugin_import, "remove_plugin",
                          return_value={"removed": True, "datastores_archived": [],
                                        "datastores_retained": []}) as rp, \
             patch.object(pack_import, "_agents_archive_root", return_value=self.data / "aa"):
            out = pack_import.remove_pack("google-pack")
        rp.assert_called_once_with("google-pack")      # `logic` is never removed
        self.assertEqual(out["plugins"], ["google-pack"])

    def test_reserved_names_still_refuse_an_import(self) -> None:
        for n in ("comms-pack", "google-pack", "base-pack"):
            with self.assertRaises(pack_import.PackImportError):
                pack_import._sanitize_pack_name(n)


class ReinstallTests(_Env):
    async def test_available_lists_only_removed_bundled_packs(self) -> None:
        names = [p["name"] for p in await packs.list_available_packs(_Req())]
        self.assertEqual(names, ["google-pack"])

    async def test_install_runs_the_trusted_update_path(self) -> None:
        with patch.object(packs, "_perform_update",
                          new=AsyncMock(return_value={"updated": "google-pack",
                                                      "version": "2.0.0"})) as pu, \
             patch("server.sdk_runtime.session.manager.drop_all",
                   new=AsyncMock(return_value=["a"])):
            out = await packs.install_first_party_pack("google-pack", _Req())
        pu.assert_awaited_once_with("google-pack", "davide")
        self.assertEqual(out["installed"], "google-pack")
        self.assertEqual(out["agents_restarted"], 1)

    async def test_install_refuses_anything_else(self) -> None:
        with patch.object(packs, "_perform_update", new=AsyncMock()) as pu:
            for n in ("comms-pack",        # installed: that is an Update
                      "base-pack",         # never removed, never reinstalled
                      "it-pack",           # not bundled: imported from its repo
                      "../etc"):
                r = await packs.install_first_party_pack(n, _Req())
                self.assertEqual(r.status_code, 400, n)
        pu.assert_not_awaited()


class SkillsOfARemovedPackTests(_Env):
    def test_installed_is_read_from_the_datadir(self) -> None:
        self.assertTrue(skill_sync.bundled_pack_installed("base-pack"))
        self.assertTrue(skill_sync.bundled_pack_installed("logic"))
        self.assertTrue(skill_sync.bundled_pack_installed("comms-pack"))
        self.assertFalse(skill_sync.bundled_pack_installed("google-pack"))

    def test_a_removed_packs_skill_is_not_resolved_from_the_image(self) -> None:
        for n in ("comms-pack", "google-pack"):
            sk = self.catalog / n / "plugins" / n / "skills" / "s1"
            sk.mkdir(parents=True)
            (sk / "SKILL.md").write_text("---\nname: s1\ndescription: d\n---\n", encoding="utf-8")
        with patch.object(skill_sync, "LOGIC_PACKS_ROOT", self.catalog), \
             patch.object(skill_sync, "DATA_CATALOG_DIR", self.data / "skills-catalog"):
            self.assertIsNotNone(skill_sync._resolve_skill_source("comms-pack/s1"))
            self.assertIsNone(skill_sync._resolve_skill_source("google-pack/s1"))
            self.assertEqual(skill_sync._pack_skill_names("google-pack"), [])
            self.assertEqual(skill_sync._pack_skill_names("comms-pack"), ["comms-pack/s1"])


if __name__ == "__main__":
    unittest.main()
