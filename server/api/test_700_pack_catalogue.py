"""clodia-platform#700 — the pack catalogue: every public pack, its state
here, and a visible `deprecated` tag.

The GitHub read (`_fetch_clodia_packs`) and the bundled catalogue are
replaced by fixtures; the installed packs are real files in a temp datadir.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

from . import gateway_pdp, pack_catalogue as pc, pack_import, packs

PUBLIC = {
    "it-pack": {"name": "it-pack", "version": "0.26.0", "description": "IT"},
    "business-pack": {"name": "business-pack", "version": "0.13.0"},
    "studio-legale": {"name": "studio-legale", "version": "0.13.0"},
    "old-pack": {"name": "old-pack", "version": "1.0.0",
                 "deprecated": {"reason": "skills moved", "replacement": "business-pack"}},
    "older-pack": {"name": "older-pack", "version": "1.0.0", "deprecated": True},
}
BUNDLED = {
    "base-pack": {"name": "base-pack", "version": "7.53.0",
                  "upstream": {"repo": "r-clodia/clodia-logic",
                               "path": "catalogs/packs/base-pack", "ref": "main"}},
}


def _up(repo: str, path: str) -> dict:
    return {"repo": repo, "path": path, "ref": "main"}


class _Datadir(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (patch.object(pack_import, "PACKS_META_DIR", self.dir),
                  patch.object(pc, "_bundled", lambda: dict(BUNDLED))):
            p.start()
            self.addCleanup(p.stop)
        pc.clear_cache()
        self.addCleanup(pc.clear_cache)

    def install(self, name: str, version: str, upstream: dict | None = None, **extra):
        d = self.dir / name
        d.mkdir()
        m = {"name": name, "version": version, **extra}
        if upstream:
            m["upstream"] = upstream
        (d / "pack.yaml").write_text(yaml.safe_dump(m), encoding="utf-8")

    def states(self, fetch=lambda: dict(PUBLIC)) -> dict:
        with patch.object(pc, "_fetch_clodia_packs", fetch):
            out = pc.entries(force=True)
        self.out = out
        return {r["name"]: r for r in out["packs"]}


class StatesTests(_Datadir):
    def test_every_state(self) -> None:
        self.install("it-pack", "0.25.0", _up(pc.REPO, "packs/it-pack"))
        self.install("business-pack", "0.13.0", _up(pc.REPO, "packs/business-pack"))
        self.install("old-pack", "0.9.0", _up(pc.REPO, "packs/old-pack"))
        self.install("comms-pack", "2.8.0", _up(pc.FIRST_PARTY_REPO, "catalogs/packs/comms-pack"))
        self.install("editorial-pack", "1.0.0", _up(pc.REPO, "packs/editorial-pack"))
        self.install("tomato", "0.25.0", _up("r-clodia/tomato-pack", ""))
        self.install("base-pack", "7.52.0", BUNDLED["base-pack"]["upstream"])
        r = self.states()
        self.assertEqual(r["it-pack"]["state"], "update_available")
        self.assertEqual((r["it-pack"]["installed_version"], r["it-pack"]["version"]),
                         ("0.25.0", "0.26.0"))
        self.assertEqual(r["business-pack"]["state"], "installed")
        self.assertEqual(r["studio-legale"]["state"], "available")
        self.assertTrue(r["studio-legale"]["installable"])
        self.assertEqual(r["old-pack"]["state"], "deprecated")
        self.assertTrue(r["old-pack"]["installed"])
        self.assertEqual(r["old-pack"]["deprecated"]["replacement"], "business-pack")
        self.assertEqual(r["older-pack"]["state"], "deprecated")
        self.assertFalse(r["older-pack"]["installed"])
        self.assertFalse(r["older-pack"]["installable"])
        self.assertEqual(r["older-pack"]["deprecated"],
                         {"reason": "", "replacement": "", "since": ""})
        self.assertEqual(r["comms-pack"]["state"], "removed_from_catalogue")
        self.assertEqual(r["editorial-pack"]["state"], "removed_from_catalogue")
        self.assertEqual((r["tomato"]["state"], r["tomato"]["source"]), ("installed", "other"))
        self.assertEqual((r["base-pack"]["state"], r["base-pack"]["source"]),
                         ("update_available", "first-party"))
        self.assertEqual(self.out["catalogue_error"], "")

    def test_an_installed_pack_may_declare_itself_deprecated(self) -> None:
        self.install("business-pack", "0.13.0", _up(pc.REPO, "packs/business-pack"),
                     deprecated={"reason": "x"})
        self.assertEqual(self.states()["business-pack"]["state"], "deprecated")

    def test_a_github_failure_is_soft(self) -> None:
        self.install("it-pack", "0.25.0", _up(pc.REPO, "packs/it-pack"))

        def boom():
            raise RuntimeError("rate limited")
        r = self.states(boom)
        self.assertIn("non raggiungibile", self.out["catalogue_error"])
        # Not knowing the catalogue is not «removed from it».
        self.assertEqual(r["it-pack"]["state"], "installed")
        self.assertEqual(r["it-pack"]["source"], "unknown")
        self.assertIn("base-pack", r)          # the bundled ones are still there

    def test_a_failure_after_a_good_read_serves_the_last_copy(self) -> None:
        self.states()

        def boom():
            raise RuntimeError("down")
        r = self.states(boom)
        self.assertEqual(r["studio-legale"]["state"], "available")
        self.assertTrue(self.out["catalogue_error"])


class InstallTests(_Datadir, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        p = patch.object(gateway_pdp, "require_authz_async",
                         new=AsyncMock(return_value="davide"))
        p.start()
        self.addCleanup(p.stop)
        p2 = patch.object(pc, "_fetch_clodia_packs", lambda: dict(PUBLIC))
        p2.start()
        self.addCleanup(p2.stop)
        p3 = patch.object(packs, "_restart_agents", new=AsyncMock(return_value=0))
        p3.start()
        self.addCleanup(p3.stop)

    async def test_a_deprecated_pack_is_not_installable(self) -> None:
        with patch.object(packs, "_perform_update", new=AsyncMock()) as pu:
            r = await packs.pack_catalogue_install("old-pack", object())
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"deprecato", r.body)
        self.assertIn(b"business-pack", r.body)
        pu.assert_not_awaited()

    async def test_an_available_pack_installs_from_its_catalogue_upstream(self) -> None:
        with patch.object(packs, "_perform_update",
                          new=AsyncMock(return_value={"updated": "studio-legale",
                                                      "version": "0.13.0"})) as pu:
            out = await packs.pack_catalogue_install("studio-legale", object())
        pu.assert_awaited_once()
        args, kw = pu.call_args
        self.assertEqual(args[0], "studio-legale")
        self.assertEqual(kw["up"], {"repo": pc.REPO, "path": "packs/studio-legale",
                                    "ref": "main"})
        self.assertTrue(kw["fresh"])
        self.assertEqual(out["installed"], "studio-legale")

    async def test_installed_or_unknown_packs_are_refused(self) -> None:
        self.install("business-pack", "0.13.0", _up(pc.REPO, "packs/business-pack"))
        r = await packs.pack_catalogue_install("business-pack", object())
        self.assertEqual(r.status_code, 400)
        r = await packs.pack_catalogue_install("nope-pack", object())
        self.assertEqual(r.status_code, 404)


class UpdateAllSaysWhyTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_pack_gone_from_its_catalogue_is_reported_as_such(self) -> None:
        async def _pu(name, principal):
            raise ValueError("rimosso dal catalogo: r-clodia/clodia-logic@main non contiene "
                             "più 'catalogs/packs/comms-pack'. Disinstallalo da Packs")
        with patch.object(gateway_pdp, "require_authz_async",
                          new=AsyncMock(return_value="davide")), \
                patch.object(packs, "_list_packs",
                             return_value=[{"name": "comms-pack", "has_upstream": True}]), \
                patch.object(packs, "_perform_update", side_effect=_pu), \
                patch("server.sdk_runtime.session.manager.drop_all",
                      new=AsyncMock(return_value=[])):
            out = await packs.update_all_packs(object())
        (row,) = out["packs"]
        self.assertTrue(row["removed_from_catalogue"])
        self.assertIn("Disinstallalo", row["error"])

    def test_the_tarball_check_names_the_removal(self) -> None:
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo("repo-sha/README.md")
            data = b"x"
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        class _R:
            content = buf.getvalue()

            def raise_for_status(self):
                return None
        with tempfile.TemporaryDirectory() as td, \
                patch("httpx.get", return_value=_R()), \
                self.assertRaises(packs.UpstreamPackGone) as cm:
            packs._download_upstream_tarball(
                {"repo": "r-clodia/clodia-logic", "path": "catalogs/packs/comms-pack",
                 "ref": "main"}, Path(td))
        self.assertTrue(str(cm.exception).startswith("rimosso dal catalogo"))


class DeprecationShapeTests(unittest.TestCase):
    def test_normalisation(self) -> None:
        self.assertIsNone(pc.deprecation({}))
        self.assertIsNone(pc.deprecation({"deprecated": False}))
        self.assertEqual(pc.deprecation({"deprecated": True}),
                         {"reason": "", "replacement": "", "since": ""})
        self.assertEqual(pc.deprecation({"deprecated": {"reason": "r", "since": "2026-11-18"}}),
                         {"reason": "r", "replacement": "", "since": "2026-11-18"})


if __name__ == "__main__":
    unittest.main()
