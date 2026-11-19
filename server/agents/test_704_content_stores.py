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
    def test_the_always_present_stores_are_guarded_and_say_why(self):
        stores = B.content_stores()
        names = {Path(p).name for p in stores}
        self.assertTrue({"activity", "transcripts", "reasoning", "logs"} <= names)
        for p, why in stores.items():
            self.assertGreater(len(why), 20)

    def test_every_content_store_of_the_review_is_listed(self):
        """#703 review: anything holding message, prompt or reply text."""
        d = Path(self.d.name)
        for rel in ("data/ophelia-sessions", "data/sysadmin-sessions", "jobs",
                    "job_proposals", "mail-courier-state", "mail-triggers",
                    "channel-relay-state", "routing", "claude-home",
                    "agent-workspaces", "data/codex-home", "runtime/opencode-data",
                    "spawns"):
            (d / rel).mkdir(parents=True, exist_ok=True)
        specs = {p: m for p, m, _c, _w in B.store_specs()}
        for rel, mode in (("data/ophelia-sessions", B.PRIVATE),
                          ("data/sysadmin-sessions", B.PRIVATE), ("jobs", B.PRIVATE),
                          ("job_proposals", B.PRIVATE), ("mail-courier-state", B.PRIVATE),
                          ("mail-triggers", B.PRIVATE), ("channel-relay-state", B.PRIVATE),
                          ("routing", B.PRIVATE), ("claude-home", B.PRIVATE),
                          ("agent-workspaces", B.PRIVATE), ("data/codex-home", B.GROUP),
                          ("runtime/opencode-data", B.GROUP), ("spawns", B.SPAWNS)):
            self.assertEqual(specs.get(str(d / rel)), mode, rel)
        from ..config import WORKSPACE_ROOT
        self.assertIn(("@bundle/sessions", B.PRIVATE),
                      {(r, m) for r, m, _c, _w in B._STORES})
        self.assertTrue(str(WORKSPACE_ROOT))

    def test_missing_optional_stores_are_not_created(self):
        with patch.object(B.os, "geteuid", lambda: 0):
            B.harden_content_stores()
        self.assertFalse((Path(self.d.name) / "claude-home").exists())
        self.assertFalse((Path(self.d.name) / "jobs").exists())

    def test_what_spawns_need_stays_reachable(self):
        """Hardening must not lock a spawn out of its own dir or of the group
        stores it shares; nor touch plugins/, packs/, agency-shared/."""
        d = Path(self.d.name)
        own = d / "spawns" / "sysadmin-1"
        own.mkdir(parents=True, mode=0o700)
        os.chmod(d / "spawns", 0o755)
        oc = d / "runtime" / "opencode-data" / "segretario"
        oc.mkdir(parents=True)
        os.chmod(oc, 0o2775)
        codex = d / "data" / "codex-home"
        codex.mkdir(parents=True)
        os.chmod(codex, 0o3775)
        for rel in ("plugins", "packs", "agency-shared", "skills-catalog"):
            (d / rel).mkdir()
            os.chmod(d / rel, 0o755)
        # The special bits as the filesystem kept them (a non-root chmod may
        # drop setgid): the hardening must keep whatever is there.
        before_oc = stat.S_IMODE(os.stat(oc).st_mode)
        before_codex = stat.S_IMODE(os.stat(codex).st_mode)
        with patch.object(B.os, "geteuid", lambda: 0):
            B.harden_content_stores()
        mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)  # noqa: E731
        self.assertEqual(mode(d / "spawns"), 0o711, "traverse-only, not 700")
        self.assertEqual(mode(own), 0o700)
        self.assertEqual(mode(oc), before_oc & ~0o007, "the seed's group keeps rw + setgid")
        self.assertEqual(mode(codex), before_codex & ~0o007, "codex group + sticky kept")
        self.assertEqual(mode(oc) & 0o070, 0o070)
        for rel in ("plugins", "packs", "agency-shared", "skills-catalog"):
            self.assertEqual(mode(d / rel), 0o755, rel)

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


class RealProbeTests(_Datadir):
    """The probe with a REAL fork, pipe and reads; only the uid change is
    replaced (`_drop`), since a test cannot setuid. Run as the file's owner,
    so «readable» and «mode 000» are both true answers."""

    def test_a_readable_dir_is_reported_readable(self):
        p = Path(self.d.name) / "open"
        p.mkdir()
        with patch.object(B, "_drop", lambda uid, gid: None):
            self.assertIs(B._legge(str(p), os.getuid()), True)
        self.assertFalse((p / ".boundary-probe").exists(), "sentinel removed")

    def test_a_closed_dir_is_reported_denied(self):
        if os.geteuid() == 0:
            self.skipTest("root reads mode 000")
        p = Path(self.d.name) / "closed"
        p.mkdir()
        os.chmod(p, 0)
        self.addCleanup(os.chmod, p, 0o700)
        with patch.object(B, "_drop", lambda uid, gid: None):
            self.assertIs(B._legge(str(p), os.getuid()), False)

    def test_a_failed_privilege_drop_is_unknown_not_safe(self):
        p = Path(self.d.name) / "x"
        p.mkdir()

        def boom(uid, gid):
            raise PermissionError("no setuid")
        with patch.object(B, "_drop", boom):
            self.assertIsNone(B._legge(str(p), 60000))

    def test_spawn_dirs_are_probed_as_a_non_owner(self):
        d = Path(self.d.name) / "spawns"
        (d / "clodia-1").mkdir(parents=True)
        seen = []
        with patch.object(B, "_legge", lambda p, u, **k: seen.append((p, u)) or False):
            self.assertIs(B._probe(str(d), B.SPAWNS, os.getuid()), False)
        owner = os.stat(d / "clodia-1").st_uid
        self.assertEqual(seen[0][0], str(d))
        self.assertNotEqual(seen[1][1], owner)


class CheckContentTests(_Datadir):
    def test_a_readable_store_is_an_error(self):
        with patch.object(B.os, "geteuid", lambda: 0), \
                patch.object(B, "_spawn_uid", lambda: 60000), \
                patch.object(B, "_probe", lambda p, m, u: True), \
                self.assertLogs("agent-server.agents.boundary", "ERROR") as log:
            esiti = B.check_content()
        self.assertTrue(all(v is True for v in esiti.values()))
        self.assertIn("CONTENUTI LEGGIBILI", "".join(log.output))
        self.assertIn("volume Docker", "".join(log.output))

    def test_unverifiable_is_not_safe_and_denied_is_the_goal(self):
        with patch.object(B.os, "geteuid", lambda: 0), \
                patch.object(B, "_spawn_uid", lambda: 60000), \
                patch.object(B, "_probe", lambda p, m, u: None), \
                self.assertLogs("agent-server.agents.boundary", "WARNING"):
            self.assertTrue(all(v is None for v in B.check_content().values()))
        with patch.object(B.os, "geteuid", lambda: 0), \
                patch.object(B, "_spawn_uid", lambda: 60000), \
                patch.object(B, "_probe", lambda p, m, u: False):
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
