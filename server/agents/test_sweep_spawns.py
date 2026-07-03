import os, tempfile, time, unittest
from pathlib import Path
from . import workspace as W


class SweepOrphanSpawnsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = W.SPAWNS_ROOT
        W.SPAWNS_ROOT = Path(self._tmp.name)

    def tearDown(self):
        W.SPAWNS_ROOT = self._orig
        self._tmp.cleanup()

    def _mk(self, name):
        d = W.SPAWNS_ROOT / name
        (d / "scratch").mkdir(parents=True)
        return d

    def test_removes_orphans_keeps_live(self):
        live = self._mk("clodia-1")
        orphan = self._mk("clodia-2")
        removed = W.sweep_orphan_spawns(live_dirs={str(live)})
        self.assertIn("clodia-2", removed)
        self.assertNotIn("clodia-1", removed)
        self.assertTrue(live.is_dir())
        self.assertFalse(orphan.is_dir())

    def test_boot_sweep_removes_all(self):
        self._mk("a-1"); self._mk("b-3")
        removed = W.sweep_orphan_spawns(set(), 0.0)
        self.assertEqual(sorted(removed), ["a-1", "b-3"])
        self.assertEqual(list(W.SPAWNS_ROOT.iterdir()), [])

    def test_min_age_protects_recent(self):
        self._mk("fresh-1")
        removed = W.sweep_orphan_spawns(set(), min_age_seconds=3600)
        self.assertEqual(removed, [])   # appena creato → protetto

    def test_memory_symlink_not_followed(self):
        mem_tmp = tempfile.TemporaryDirectory(); self.addCleanup(mem_tmp.cleanup)
        real_mem = Path(mem_tmp.name) / "realmem"
        real_mem.mkdir(); (real_mem / "keep.txt").write_text("x")
        d = self._mk("clodia-9")
        (d / ".agent").mkdir()
        os.symlink(str(real_mem), str(d / ".agent" / "memory"))
        W.sweep_orphan_spawns(set(), 0.0)
        self.assertFalse(d.is_dir())
        self.assertTrue((real_mem / "keep.txt").is_file())  # memory reale preservata


if __name__ == "__main__":
    unittest.main()
