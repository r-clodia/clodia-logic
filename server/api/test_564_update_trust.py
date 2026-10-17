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


if __name__ == "__main__":
    unittest.main()
