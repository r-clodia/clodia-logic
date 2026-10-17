"""clodia-platform#563 — disable / enable a pack without removing it.

`disabled: true` in the installed manifest turns a pack off. Measured here:

1. the flag: only a real `true` counts, base-pack can never be disabled, the
   native agents are never switched off by a pack;
2. what stops: the pack's agents are not loaded, its skills and rules are not
   served (data catalog and image), its MCP backends are unmounted;
3. the endpoints: admin-only, base-pack refused, idempotent, enable mounts
   again (trusted only for a first-party upstream);
4. an Update keeps a disabled pack disabled and mounts/refreshes nothing;
5. the list reports `disabled` / `disableable`.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from ..agents import loader, pack_state, rule_sync, skill_sync
from . import gateway_pdp, pack_deprovision, pack_import, packs


class _Req:
    headers: dict = {}


def _manifest(root: Path, name: str, **extra) -> Path:
    d = root / "packs" / name
    d.mkdir(parents=True, exist_ok=True)
    m = {"name": name, "version": "1.0.0", "agents": [f"{name}-agent", "messaggero"],
         "plugins": [name], **extra}
    p = d / "pack.yaml"
    p.write_text(yaml.safe_dump(m), encoding="utf-8")
    return p


class _Env(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        _manifest(self.data, "acme-pack", upstream={"repo": "r-clodia/clodia-packs",
                                                     "path": "packs/acme-pack"})
        _manifest(self.data, "base-pack")
        dp = lambda rel: self.data / rel  # noqa: E731
        for p in (
            patch.object(pack_state, "data_path", side_effect=dp),
            patch.object(skill_sync, "data_path", side_effect=dp),
            patch.object(pack_import, "PACKS_META_DIR", self.data / "packs"),
            patch.object(gateway_pdp, "require_authz_async", new=AsyncMock(return_value="davide")),
            patch.object(gateway_pdp, "is_agent", side_effect=lambda n: n != "davide"),
            patch("server.sdk_runtime.session.manager.drop_all", new=AsyncMock(return_value=["a"])),
            patch.object(packs.plugins_api, "invalidate_plugins"),
            patch.object(packs.registry, "load"),
        ):
            p.start()
            self.addCleanup(p.stop)


class FlagTests(_Env):
    def test_only_true_disables(self) -> None:
        for v in ("true", 1, "yes", False):
            _manifest(self.data, "acme-pack", disabled=v)
            self.assertFalse(pack_state.is_disabled("acme-pack"), v)
        _manifest(self.data, "acme-pack", disabled=True)
        self.assertTrue(pack_state.is_disabled("acme-pack"))

    def test_base_pack_is_never_disabled(self) -> None:
        _manifest(self.data, "base-pack", disabled=True)
        self.assertFalse(pack_state.is_disabled("base-pack"))
        self.assertNotIn("base-pack", pack_state.disabled_packs())
        with self.assertRaises(ValueError):
            pack_state.set_disabled("base-pack", True)

    def test_natives_are_never_switched_off(self) -> None:
        _manifest(self.data, "acme-pack", disabled=True)
        self.assertEqual(pack_state.disabled_agents(), {"acme-pack-agent"})
        self.assertEqual(pack_state.disabled_plugins(), {"acme-pack"})

    def test_natives_mirror_pack_import(self) -> None:
        self.assertEqual(set(pack_state.NATIVE_AGENTS), set(pack_import._NATIVE_AGENTS))
        self.assertEqual(set(pack_state.NEVER_DISABLED),
                         set(pack_import.NON_REMOVABLE_PACK_NAMES))

    def test_set_disabled_round_trip(self) -> None:
        pack_state.set_disabled("acme-pack", True)
        self.assertTrue(pack_state.is_disabled("acme-pack"))
        pack_state.set_disabled("acme-pack", False)
        doc = yaml.safe_load((self.data / "packs/acme-pack/pack.yaml").read_text())
        self.assertNotIn("disabled", doc)
        self.assertEqual(doc["version"], "1.0.0")   # nothing else touched
        with self.assertRaises(KeyError):
            pack_state.set_disabled("nobody", True)


class WhatStopsTests(_Env):
    def test_agents_of_a_disabled_pack_are_not_loaded(self) -> None:
        agents = self.data / "agents"
        for n in ("acme-pack-agent", "messaggero"):
            (agents / n).mkdir(parents=True)
            (agents / n / "agent.yaml").write_text(yaml.safe_dump({
                "name": n, "display_name": n, "description": "x", "type": "normal",
                "system_prompt": "system-prompt.md", "capabilities": []}), encoding="utf-8")
            (agents / n / "system-prompt.md").write_text("x", encoding="utf-8")
        reg = loader.AgentRegistry(agents)
        reg.load()
        self.assertIn("acme-pack-agent", {s.name for s in reg.list()})
        _manifest(self.data, "acme-pack", disabled=True)
        reg.load()
        names = {s.name for s in reg.list()}
        self.assertNotIn("acme-pack-agent", names)
        self.assertIn("messaggero", names)          # native: never switched off
        self.assertTrue((agents / "acme-pack-agent" / "agent.yaml").is_file())  # kept

    def test_skills_and_rules_are_not_served(self) -> None:
        sk = self.data / "skills-catalog" / "acme-pack" / "s1"
        sk.mkdir(parents=True)
        (sk / "SKILL.md").write_text("---\nname: s1\ndescription: d\n---\n", encoding="utf-8")
        rl = self.data / "rules-catalog" / "acme-pack"
        rl.mkdir(parents=True)
        (rl / "r1.md").write_text("---\nname: r1\ndescription: d\n---\nbody\n", encoding="utf-8")
        with patch.object(skill_sync, "DATA_CATALOG_DIR", self.data / "skills-catalog"), \
             patch.object(rule_sync, "DATA_CATALOG_DIR", self.data / "rules-catalog"):
            self.assertIsNotNone(skill_sync._resolve_skill_source("acme-pack/s1"))
            self.assertIsNotNone(skill_sync._resolve_skill_source("s1"))
            self.assertEqual(skill_sync._pack_skill_names("acme-pack"), ["acme-pack/s1"])
            on_rule = rule_sync._resolve_rule_source("acme-pack/r1")
            self.assertIsNotNone(on_rule)
            _manifest(self.data, "acme-pack", disabled=True)
            self.assertIsNone(skill_sync._resolve_skill_source("acme-pack/s1"))
            self.assertIsNone(skill_sync._resolve_skill_source("s1"))
            self.assertEqual(skill_sync._pack_skill_names("acme-pack"), [])
            self.assertNotIn("s1", skill_sync._all_skill_names())
            self.assertIsNone(rule_sync._resolve_rule_source("acme-pack/r1"))
            self.assertEqual(rule_sync._pack_rule_names("acme-pack"), [])

    def test_a_disabled_bundled_pack_is_not_served_from_the_image(self) -> None:
        _manifest(self.data, "comms-pack")
        self.assertTrue(skill_sync.bundled_pack_installed("comms-pack"))
        _manifest(self.data, "comms-pack", disabled=True)
        self.assertFalse(skill_sync.bundled_pack_installed("comms-pack"))


class EndpointTests(_Env):
    CFG = {"command": "acme-server", "args": []}

    def setUp(self) -> None:
        super().setUp()
        self.mounted = [{"name": "acme-mcp", "pack": "acme-pack",
                         "config_sha256": pack_state.config_hash(self.CFG)},
                        {"name": "other", "pack": "someone-else"}]
        for p in (
            patch.object(pack_deprovision, "snapshot_pack",
                         return_value={"acme-pack": {"mcp_servers": ["acme-mcp", "pending-one", "other"]}}),
            patch.object(packs.gateway_pdp, "gw_tool",
                         side_effect=lambda t, a, pr: (200, {"result": {"mcp_backends": self.mounted}})),
            patch.object(packs.pack_mcp_mount, "_manifest_mcp",
                         side_effect=lambda plugin: {"acme-mcp": self.CFG, "pending-one": {"command": "x"},
                                                     "other": {"command": "y"}}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _unmount(self, unmounted, failed=()):
        return patch.object(pack_deprovision, "_unmount",
                            return_value={"unmounted": list(unmounted), "failed": list(failed)})

    async def test_disable_unmounts_only_its_own_mounted_backends(self) -> None:
        with self._unmount(["acme-mcp"]) as um:
            out = await packs.disable_pack("acme-pack", _Req())
        # not `pending-one` (never mounted), not `other` (another pack's)
        um.assert_called_once_with(["acme-mcp"], "davide")
        self.assertEqual(out["mcp_not_unmounted"], ["other"])
        self.assertTrue(pack_state.is_disabled("acme-pack"))
        self.assertEqual(out["agents_restarted"], 1)
        self.assertEqual(set(pack_state.take_unmounted("acme-pack")), {"acme-mcp"})

    async def test_unreadable_gateway_unmounts_nothing(self) -> None:
        with patch.object(packs.gateway_pdp, "gw_tool", return_value=(503, {})), \
             self._unmount([]) as um:
            await packs.disable_pack("acme-pack", _Req())
        um.assert_not_called()

    async def test_a_failed_unmount_is_retried_by_a_second_disable(self) -> None:
        with self._unmount([], [{"server": "acme-mcp"}]):
            out = await packs.disable_pack("acme-pack", _Req())
        self.assertIn("warning", out)
        with self._unmount(["acme-mcp"]) as um:
            again = await packs.disable_pack("acme-pack", _Req())
        um.assert_called_once()                       # retried, not «already done»
        self.assertTrue(again["already"])
        self.assertEqual(set(pack_state.take_unmounted("acme-pack")), {"acme-mcp"})

    async def test_base_pack_and_unknown_are_refused(self) -> None:
        self.assertEqual((await packs.disable_pack("base-pack", _Req())).status_code, 403)
        self.assertEqual((await packs.disable_pack("nobody", _Req())).status_code, 404)
        self.assertEqual((await packs.enable_pack("nobody", _Req())).status_code, 404)

    async def test_an_agent_can_neither_disable_nor_enable(self) -> None:
        with patch.object(gateway_pdp, "require_authz_async", new=AsyncMock(return_value="sysadmin")):
            self.assertEqual((await packs.disable_pack("acme-pack", _Req())).status_code, 403)
            pack_state.set_disabled("acme-pack", True)
            self.assertEqual((await packs.enable_pack("acme-pack", _Req())).status_code, 403)
        self.assertTrue(pack_state.is_disabled("acme-pack"))

    async def test_enable_mounts_back_only_what_disable_unmounted(self) -> None:
        with self._unmount(["acme-mcp"]):
            await packs.disable_pack("acme-pack", _Req())
        with patch.object(packs.pack_mcp_mount, "auto_mount_imported_mcp") as am:
            out = await packs.enable_pack("acme-pack", _Req())
        self.assertFalse(pack_state.is_disabled("acme-pack"))
        args, kw = am.call_args
        self.assertEqual(args[0]["plugins"], [{"plugin": "acme-pack"}])
        self.assertEqual(kw["only"], {"acme-mcp"})
        self.assertTrue(kw["trusted"])
        self.assertEqual(out["enabled"], "acme-pack")
        self.assertEqual(pack_state.take_unmounted("acme-pack"), {})   # consumed

    async def test_a_declared_config_never_mounted_is_not_mounted_back(self) -> None:
        # Re-imported while ENABLED with a new command: the new config stayed
        # pending, the reviewed one is what runs. Disable+enable must not
        # mount the unreviewed one.
        self.mounted[0]["config_sha256"] = pack_state.config_hash({"command": "reviewed"})
        with self._unmount(["acme-mcp"]):
            out = await packs.disable_pack("acme-pack", _Req())
        self.assertEqual(out["mcp_review_on_enable"], ["acme-mcp"])
        with patch.object(packs.pack_mcp_mount, "auto_mount_imported_mcp") as am:
            await packs.enable_pack("acme-pack", _Req())
        am.assert_not_called()

    async def test_a_server_changed_while_disabled_is_not_mounted_back(self) -> None:
        with self._unmount(["acme-mcp"]):
            await packs.disable_pack("acme-pack", _Req())
        self.CFG = {"command": "something-else"}         # a re-import changed it
        with patch.object(packs.pack_mcp_mount, "auto_mount_imported_mcp") as am:
            out = await packs.enable_pack("acme-pack", _Req())
        am.assert_not_called()
        self.assertEqual(out["mcp_pending"], ["acme-mcp"])

    async def test_disable_enable_does_not_mount_pending_servers(self) -> None:
        # Nothing of the pack is mounted (its servers stayed `pending` at
        # import): nothing is recorded, nothing is mounted back — whatever
        # upstream its own manifest claims.
        self.mounted = []
        _manifest(self.data, "acme-pack", upstream={"repo": "r-clodia/clodia-packs"})
        with self._unmount([]) as um:
            await packs.disable_pack("acme-pack", _Req())
        um.assert_not_called()
        with patch.object(packs.pack_mcp_mount, "auto_mount_imported_mcp") as am:
            await packs.enable_pack("acme-pack", _Req())
        am.assert_not_called()

    def test_auto_mount_only_filters_servers(self) -> None:
        from . import pack_mcp_mount
        with patch.object(pack_mcp_mount, "_manifest_mcp",
                          return_value={"a": {"command": "x"}, "b": {"command": "y"}}), \
             patch.object(pack_mcp_mount.gateway_pdp, "gw_tool",
                          return_value=(200, {"ok": True, "result": {"registered": ["a"]}})) as gw:
            pack_mcp_mount.auto_mount_imported_mcp(
                {"pack": "p", "plugins": [{"plugin": "p"}]}, "davide", trusted=True, only={"a"})
        mounted = [c.args[1]["config"]["mcpServers"] for c in gw.call_args_list]
        self.assertEqual(mounted, [{"a": {"command": "x"}}])

    def test_unreadable_manifest_is_not_rewritten(self) -> None:
        p = self.data / "packs/acme-pack/pack.yaml"
        p.write_text("agents: [a\n  : broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            pack_state.set_disabled("acme-pack", True)
        self.assertIn("broken", p.read_text())

    def test_written_manifests_stay_readable(self) -> None:
        import stat
        pack_state.set_disabled("acme-pack", True)
        mode = stat.S_IMODE((self.data / "packs/acme-pack/pack.yaml").stat().st_mode)
        self.assertEqual(mode, 0o644)


class JobsOfADisabledPackTests(_Env):
    async def test_a_job_of_a_disabled_agent_is_skipped_not_run_as_clodia(self) -> None:
        from ..scheduler import scheduler
        pack_state.set_disabled("acme-pack", True)
        with patch.object(scheduler.db, "get_job",
                          return_value={"id": 7, "agent": "acme-pack-agent", "mode": "agentic",
                                        "prompt": "x"}), \
             patch.object(scheduler.db, "mark_run") as mr:
            out = await scheduler.fire_job(7)
        self.assertEqual(out["status"], "skipped: pack disattivato")
        mr.assert_called_once()


class ReinstallKeepsItOffTests(_Env):
    def test_install_carries_the_flag_in_the_same_write(self) -> None:
        pack_state.set_disabled("acme-pack", True)
        root = self.data / "src" / "acme-pack"
        root.mkdir(parents=True)
        (root / "pack.yaml").write_text(yaml.safe_dump(
            {"name": "acme-pack", "version": "3.0.0", "agents": [], "plugins": []}),
            encoding="utf-8")
        writes = []
        real = pack_state._atomic_write

        def spy(path, text):
            writes.append(yaml.safe_load(text))
            return real(path, text)

        (root / "plugins" / "acme-pack").mkdir(parents=True)
        with patch.object(pack_state, "_atomic_write", side_effect=spy), \
             patch.object(pack_import.plugin_import, "install_plugin_from_root",
                          return_value={"plugin": "acme-pack", "status": "installed"}):
            pack_import.install_pack_from_root(root, source="test", force=True)
        manifest_writes = [w for w in writes if isinstance(w, dict) and w.get("version") == "3.0.0"]
        self.assertTrue(manifest_writes)
        self.assertTrue(all(w.get("disabled") is True for w in manifest_writes))
        self.assertTrue(pack_state.is_disabled("acme-pack"))


class UpdateKeepsItOffTests(_Env):
    async def test_update_of_a_disabled_pack(self) -> None:
        pack_state.set_disabled("acme-pack", True)

        def _install(root, **kw):
            # the Update rewrites the manifest without the flag
            _manifest(self.data, "acme-pack", version="2.0.0")
            return {"kind": "pack", "pack": "acme-pack",
                    "gateway_components": {"declared": ["x"]}}

        with patch.object(packs, "_pack_upstream",
                          return_value={"repo": "r-clodia/clodia-packs", "path": "p", "ref": "main"}), \
             patch.object(packs, "_download_upstream_tarball", return_value=Path("/x")), \
             patch.object(packs, "_check_update_root"), \
             patch.object(packs.pack_import, "install_pack_from_root", side_effect=_install), \
             patch.object(packs.pack_mcp_mount, "auto_mount_imported_mcp") as am, \
             patch.object(packs, "set_setup_pending"), \
             patch.object(packs, "_refresh_gateway_components", new=AsyncMock()) as rf:
            out = await packs._perform_update("acme-pack", "davide")
        self.assertTrue(pack_state.is_disabled("acme-pack"))
        am.assert_not_called()
        rf.assert_not_awaited()
        self.assertEqual(out["version"], "2.0.0")


if __name__ == "__main__":
    unittest.main()
