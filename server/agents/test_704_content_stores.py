"""#704 — the agent-server's content stores are root-only, and verified.

Activity log, transcripts, stored reasoning and logs hold what was written.
Until #704 the first two and the logs were created 755/644 on the shared
datadir, readable by any spawn uid. These tests cover the logic; the real
probe runs at boot as root inside the container (and logs ERROR on a macOS
virtiofs bind, where no chmod is enforced).
"""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import activity_log, boundary_check as B


class _Datadir(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        p = patch("server.config.CLODIA_DATA", Path(self.d.name))
        p.start(); self.addCleanup(p.stop)


class StoresTests(_Datadir):
    def test_the_four_stores_are_guarded_and_say_why(self):
        stores = B.content_stores()
        names = {Path(p).name for p in stores}
        self.assertEqual(names, {"activity", "transcripts", "reasoning", "logs"})
        for p, why in stores.items():
            self.assertTrue(p.startswith(self.d.name), p)
            self.assertGreater(len(why), 20)

    def test_harden_makes_dirs_700_and_files_600_without_following_links(self):
        act = Path(self.d.name) / "agent-state" / "activity" / "clodia"
        act.mkdir(parents=True, mode=0o755)
        f = act / "2026-11-18.jsonl"
        f.write_text("{}\n")
        os.chmod(f, 0o644)
        outside = Path(self.d.name) / "outside.txt"
        outside.write_text("x")
        os.chmod(outside, 0o644)
        (act / "link").symlink_to(outside)
        with patch.object(B.os, "geteuid", lambda: 0):
            out = B.harden_content_stores()
        self.assertTrue(all(v == "ok" for v in out.values()), out)
        mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)  # noqa: E731
        self.assertEqual(mode(act.parent), 0o700)
        self.assertEqual(mode(act), 0o700)
        self.assertEqual(mode(f), 0o600)
        self.assertEqual(mode(outside), 0o644, "a symlink must not be followed")
        self.assertEqual(mode(Path(self.d.name) / "logs"), 0o700)

    def test_without_root_nothing_is_changed(self):
        with patch.object(B.os, "geteuid", lambda: 1000):
            self.assertEqual(B.harden_content_stores(), {})
            self.assertEqual(B.check_content(), {})


class CheckContentTests(_Datadir):
    def test_a_readable_store_is_an_error(self):
        with patch.object(B.os, "geteuid", lambda: 0), \
                patch.object(B, "_spawn_uid", lambda: 60000), \
                patch.object(B, "_legge", lambda p, u: True), \
                self.assertLogs("agent-server.agents.boundary", "ERROR") as log:
            esiti = B.check_content()
        self.assertTrue(all(v is True for v in esiti.values()))
        self.assertIn("CONTENUTI LEGGIBILI", "".join(log.output))
        self.assertIn("volume Docker", "".join(log.output))

    def test_unverifiable_is_not_safe_and_denied_is_the_goal(self):
        with patch.object(B.os, "geteuid", lambda: 0), \
                patch.object(B, "_spawn_uid", lambda: 60000), \
                patch.object(B, "_legge", lambda p, u: None), \
                self.assertLogs("agent-server.agents.boundary", "WARNING"):
            self.assertTrue(all(v is None for v in B.check_content().values()))
        with patch.object(B.os, "geteuid", lambda: 0), \
                patch.object(B, "_spawn_uid", lambda: 60000), \
                patch.object(B, "_legge", lambda p, u: False):
            self.assertTrue(all(v is False for v in B.check_content().values()))

    def test_it_runs_at_boot_after_hardening(self):
        import inspect
        from .. import main as M
        src = inspect.getsource(M)
        self.assertLess(src.index("harden_content_stores()"), src.index("check_content()"))


class NewFilesArePrivateTests(unittest.TestCase):
    def test_an_activity_file_is_born_600_in_a_700_dir(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.object(activity_log, "ACTIVITY_DIR", Path(d) / "activity"):
            activity_log.append("sysadmin", "run_started", {"prompt": "x"})
            files = list((Path(d) / "activity" / "sysadmin").glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            self.assertEqual(stat.S_IMODE(os.stat(files[0]).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(files[0].parent).st_mode) & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
