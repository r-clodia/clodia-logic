"""clodia-platform#564 — a pack Update trusts where the code comes from, not
what the pack says about itself.

Before: `_perform_update` fetched from the installed manifest's `upstream`
(written by the pack itself at import), installed with `allow_reserved=True,
force=True`, and mounted its MCP servers as trusted. A third-party pack could
point its upstream at its author's repository and, at the next Update, ship a
`pack.yaml` named `comms-pack` — or simply get its servers mounted without the
opt-in an import requires.

Measured here:

1. first-party = a repository of the platform's GitHub owner, nothing else;
2. a third-party upstream: installed without reserved names, servers not
   trusted (pending);
3. a reserved name is updated only from a first-party repository, and from the
   bundled catalog's declaration rather than the datadir's;
4. the fetched upstream must contain exactly the pack being updated.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import unittest.mock
from unittest.mock import AsyncMock, patch

import yaml

from . import pack_import, packs


def _pack(root: Path, name: str) -> Path:
    d = root / "src"
    d.mkdir(parents=True, exist_ok=True)
    (d / "pack.yaml").write_text(yaml.safe_dump({"name": name, "version": "9.9.9"}),
                                 encoding="utf-8")
    (d / "plugins" / name).mkdir(parents=True, exist_ok=True)
    return d


class _Env(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.fetched = "acme-pack"
        self.installs: list[dict] = []
        self.mounts: list[bool] = []

        def download(up, td):
            return _pack(Path(td), self.fetched)

        def install(root, **kw):
            self.installs.append(kw)
            return {"kind": "pack", "pack": self.fetched}

        def mount(result, principal, trusted=False, **kw):
            self.mounts.append(trusted)

        for p in (
            patch.object(packs, "_download_upstream_tarball", side_effect=download),
            patch.object(packs.pack_import, "install_pack_from_root", side_effect=install),
            patch.object(packs.pack_mcp_mount, "auto_mount_imported_mcp", side_effect=mount),
            patch.object(packs.plugins_api, "invalidate_plugins"),
            patch.object(packs.registry, "load"),
            patch.object(packs, "set_setup_pending"),
            patch.object(packs, "_refresh_gateway_components", new=AsyncMock()),
            patch.object(packs.pack_state, "is_disabled", return_value=False),
            patch.object(pack_import, "PACKS_META_DIR", self.base / "packs"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _upstream(self, repo: str, path: str = "x"):
        return patch.object(packs, "_pack_upstream",
                            return_value={"repo": repo, "path": path, "ref": "main"})


class FirstPartyTests(unittest.TestCase):
    def test_first_party_is_the_platform_owner_only(self) -> None:
        yes = ("r-clodia/clodia-packs", "r-clodia/clodia-logic", "R-Clodia/tomato-pack")
        no = ("someone/r-clodia", "r-clodia-evil/clodia-packs", "r-clodia",
              "r-clodia/a/b", "", None)
        for r in yes:
            self.assertTrue(packs._is_first_party_upstream({"repo": r}), r)
        for r in no:
            self.assertFalse(packs._is_first_party_upstream({"repo": r}), r)


class UpdateTrustTests(_Env):
    async def test_a_third_party_upstream_is_not_trusted(self) -> None:
        with self._upstream("evil/acme"):
            await packs._perform_update("acme-pack", "davide")
        self.assertEqual(self.installs[0]["allow_reserved"], False)
        self.assertEqual(self.mounts, [False])     # servers stay pending

    async def test_a_first_party_upstream_is_trusted(self) -> None:
        with self._upstream("r-clodia/clodia-packs"):
            await packs._perform_update("acme-pack", "davide")
        self.assertEqual(self.mounts, [True])
        self.assertEqual(self.installs[0]["allow_reserved"], False)   # not a reserved name

    async def test_a_third_party_upstream_cannot_ship_a_reserved_name(self) -> None:
        self.fetched = "comms-pack"
        with self._upstream("evil/acme"), self.assertRaises(ValueError) as cm:
            await packs._perform_update("acme-pack", "davide")
        self.assertIn("comms-pack", str(cm.exception))
        self.assertEqual(self.installs, [])

    async def test_an_update_never_installs_another_pack(self) -> None:
        self.fetched = "other-pack"
        with self._upstream("r-clodia/clodia-packs"), self.assertRaises(ValueError):
            await packs._perform_update("acme-pack", "davide")
        self.assertEqual(self.installs, [])

    async def test_a_reserved_name_only_from_first_party(self) -> None:
        self.fetched = "editorial-pack"     # reserved, not bundled
        with self._upstream("evil/acme"), self.assertRaises(ValueError):
            await packs._perform_update("editorial-pack", "davide")
        self.assertEqual(self.installs, [])

    async def test_a_bundled_reserved_pack_uses_the_bundled_upstream(self) -> None:
        self.fetched = "comms-pack"
        # the datadir copy was tampered with: the bundled declaration wins
        with self._upstream("evil/acme") as pu:
            await packs._perform_update("comms-pack", "davide")
        pu.assert_not_called()
        self.assertEqual(self.installs[0]["allow_reserved"], True)
        self.assertEqual(self.installs[0]["source"], "github:r-clodia/clodia-logic")
        self.assertEqual(self.mounts, [True])


class UpstreamValidationTests(unittest.TestCase):
    def test_ref_and_repo_cannot_traverse(self) -> None:
        ok = {"repo": "r-clodia/clodia-packs", "ref": "main", "path": "packs/it-pack"}
        self.assertTrue(packs._valid_upstream(ok))
        self.assertTrue(packs._valid_upstream({**ok, "ref": "feature/x-1.2"}))
        for bad in ({"ref": "x/../../../../attacker/evil/tarball/main"},
                    {"ref": "/main"}, {"ref": "main?x=1"}, {"ref": "ma%2e%2e"},
                    {"repo": "r-clodia/.."}, {"repo": "r-clodia/."},
                    {"repo": "r-clodia/a/b"}, {"repo": "r-clodia"},
                    {"path": "../../etc"}, {"path": "packs/../../x"}):
            self.assertFalse(packs._valid_upstream({**ok, **bad}), bad)

    def test_a_bad_upstream_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "packs" / "evil"
            d.mkdir(parents=True)
            (d / "pack.yaml").write_text(yaml.safe_dump({"upstream": {
                "repo": "r-clodia/clodia-packs",
                "ref": "x/../../../../attacker/evil/tarball/main"}}), encoding="utf-8")
            with patch.object(pack_import, "PACKS_META_DIR", Path(td) / "packs"), \
                 patch.object(packs, "workspace_path", side_effect=lambda r: Path(td) / "ws" / r):
                self.assertIsNone(packs._pack_upstream("evil"))


class TarballTests(unittest.TestCase):
    def _tar(self, members: dict, link: tuple | None = None) -> bytes:
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            if link:
                info = tarfile.TarInfo(link[0])
                info.type = tarfile.SYMTYPE
                info.linkname = link[1]
                tf.addfile(info)
        return buf.getvalue()

    def _download(self, content: bytes, path: str):
        resp = unittest.mock.MagicMock(content=content)
        resp.raise_for_status = lambda: None
        with tempfile.TemporaryDirectory() as td, \
             patch("httpx.get", return_value=resp), \
             patch.object(packs, "_github_token", return_value=None):
            return packs._download_upstream_tarball(
                {"repo": "r-clodia/clodia-packs", "ref": "main", "path": path}, Path(td))

    def test_a_symlink_in_the_archive_is_refused(self) -> None:
        data = self._tar({"top/packs/a/pack.yaml": b"name: a\n"},
                         link=("top/packs/a/leak", "/etc/passwd"))
        with self.assertRaises(pack_import.PackImportError):
            self._download(data, "packs/a")

    def test_a_path_cannot_leave_the_archive(self) -> None:
        data = self._tar({"top/packs/a/pack.yaml": b"name: a\n"})
        # `_valid_upstream` already refuses `..`; the confinement is the
        # second wall, checked here by bypassing the first.
        with patch.object(packs, "_valid_upstream", return_value=True), \
             self.assertRaises(pack_import.PackImportError):
            self._download(data, "../../..")


class SeedForceTests(unittest.TestCase):
    def test_a_third_party_update_replaces_only_its_own_seeds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            agents = base / "agents"
            for n in ("clodia", "acme-agent", "foreign"):
                (agents / n).mkdir(parents=True)
                (agents / n / "agent.yaml").write_text(f"name: {n}\nold: true\n", encoding="utf-8")
            src = base / "src"
            for n in ("clodia", "acme-agent", "foreign"):
                (src / n).mkdir(parents=True)
                (src / n / "agent.yaml").write_text(f"name: {n}\nold: false\n", encoding="utf-8")
            with patch.object(pack_import.registry, "base_dir", agents), \
                 patch.object(pack_import, "_register_seed", create=True), \
                 patch.object(pack_import, "_pki_issue", create=True):
                out = {n: pack_import._install_seed(src / n, force=True, force_only={"acme-agent"})
                       for n in ("clodia", "acme-agent", "foreign")}
            self.assertEqual(out["clodia"]["status"], "exists")
            self.assertEqual(out["foreign"]["status"], "exists")
            self.assertIn("old: true", (agents / "clodia" / "agent.yaml").read_text())
            self.assertIn("old: true", (agents / "foreign" / "agent.yaml").read_text())
            # the pack's own seed was taken (replaced, then validated by the
            # installer): not skipped as "exists"
            self.assertNotEqual(out["acme-agent"]["status"], "exists")


class OwnedSeedsTests(unittest.TestCase):
    def test_a_seed_another_pack_lists_is_not_owned(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for n, agents in (("evil", ["avvocato", "evil-bot", "clodia"]),
                              ("studio-legale", ["avvocato"])):
                (root / n).mkdir()
                (root / n / "pack.yaml").write_text(yaml.safe_dump({"agents": agents}),
                                                    encoding="utf-8")
            with patch.object(pack_import, "PACKS_META_DIR", root):
                self.assertEqual(packs._owned_seeds("evil"), {"evil-bot"})
                self.assertEqual(packs._owned_seeds("studio-legale"), set())


class UpdateAllThirdPartyTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_all_does_not_run_a_third_partys_setup(self) -> None:
        from . import gateway_pdp
        with patch.object(gateway_pdp, "require_authz_async", new=AsyncMock(return_value="davide")), \
             patch.object(packs, "_list_packs", return_value=[{"name": "acme", "has_upstream": True}]), \
             patch.object(packs, "_perform_update", new=AsyncMock(return_value={
                 "updated": "acme", "version": "1", "first_party_source": False})), \
             patch("server.api.pack_mcp_mount._plugin_names", return_value=[]), \
             patch("server.api.pack_ops.declarations", return_value={}), \
             patch("server.api.pack_ops_logical.run_logical_setup_async", new=AsyncMock()) as setup, \
             patch("server.sdk_runtime.session.manager.drop_all", new=AsyncMock(return_value=[])):
            out = await packs.update_all_packs(unittest.mock.MagicMock())
        setup.assert_not_awaited()
        self.assertFalse(out["packs"][0]["setup_done"])


if __name__ == "__main__":
    unittest.main()
