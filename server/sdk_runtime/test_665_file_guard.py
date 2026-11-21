"""clodia-platform#665 — the platform's deny list for a spawn's file tools.

Real paths: a temporary tree stands in for `/datadir` and `/clodia`, with a
secret, a credential-shaped file and a symlink to the secret from the spawn's
own scratch. The list itself is patched to point there; the logic is not.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from . import file_guard as fg
from . import session as S


class _Tree(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="fg-")).resolve()
        self.secrets = self.root / "datadir" / "secrets"
        self.secrets.mkdir(parents=True)
        (self.secrets / "token").write_text("x")
        self.bundle = self.root / "clodia"
        (self.bundle / "skills").mkdir(parents=True)
        (self.bundle / "skills" / ".env.local").write_text("x")
        (self.bundle / "skills" / "README.md").write_text("x")
        self.spawn = self.root / "datadir" / "agents" / "s-1"
        (self.spawn / "scratch").mkdir(parents=True)
        os.symlink(self.secrets / "token", self.spawn / "scratch" / "innocuo.txt")
        p1 = mock.patch.object(fg, "PROTECTED_DIRS", (str(self.secrets),))
        p2 = mock.patch.object(fg, "BUNDLE_ROOT", str(self.bundle))
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def no(self, tool, **inp):
        self.assertIsNotNone(fg.refused(tool, inp, str(self.spawn)), (tool, inp))

    def ok(self, tool, **inp):
        self.assertIsNone(fg.refused(tool, inp, str(self.spawn)), (tool, inp))


class DenyListTests(_Tree):
    def test_every_file_tool_on_a_protected_path(self):
        target = str(self.secrets / "token")
        for tool, key in (("Read", "file_path"), ("Write", "file_path"),
                          ("Edit", "file_path"), ("MultiEdit", "file_path"),
                          ("NotebookEdit", "notebook_path")):
            with self.subTest(tool=tool):
                self.no(tool, **{key: target})

    def test_a_symlink_from_the_scratch_is_followed(self):
        self.no("Read", file_path=str(self.spawn / "scratch" / "innocuo.txt"))
        self.no("Read", file_path="scratch/innocuo.txt")   # relative to the cwd

    def test_dot_dot_does_not_walk_around_it(self):
        self.no("Read", file_path=str(self.spawn / ".." / ".." / "secrets" / "token"))

    def test_credential_shaped_files_in_the_bundle(self):
        self.no("Read", file_path=str(self.bundle / "skills" / ".env.local"))
        self.ok("Read", file_path=str(self.bundle / "skills" / "README.md"))

    def test_a_search_that_would_cross_a_protected_dir(self):
        self.no("Grep", pattern="token", path=str(self.root / "datadir"))
        self.no("Glob", pattern=str(self.secrets) + "/**/*")
        self.no("Glob", pattern="**/*", path=str(self.root))

    def test_the_spawns_own_work_is_untouched(self):
        self.ok("Read", file_path=str(self.spawn / "scratch" / "nota.md"))
        self.ok("Write", file_path="scratch/out.txt")
        self.ok("Grep", pattern="x")                     # the cwd
        self.ok("Glob", pattern="**/*.py")
        self.ok("Bash", command=f"cat {self.secrets}/token")   # the uid's job

    def test_a_relative_path_without_a_cwd_is_not_guessed(self):
        self.assertIsNone(fg.refused("Read", {"file_path": "x"}, None))


class TheGateAppliesIt(_Tree):
    def _ask(self, gate, tool, inp):
        return asyncio.run(gate(tool, inp, None))

    def test_the_gate_refuses_before_anything_a_seed_grants(self):
        gate = S._permission_gate([], "chan:SEAL-1:x:clodia", cwd=str(self.spawn))
        r = self._ask(gate, "Read", {"file_path": str(self.secrets / "token")})
        self.assertIsInstance(r, PermissionResultDeny)
        self.assertIn("665", r.message)
        r = self._ask(gate, "Read", {"file_path": str(self.spawn / "scratch" / "a")})
        self.assertIsInstance(r, PermissionResultAllow)


class ShellPatternsReachTheGate(unittest.TestCase):
    def test_a_seed_pattern_denies_the_command(self):
        from ..agents.models import Sandbox
        spec = type("Spec", (), {"sandbox": Sandbox(deny_shell_patterns=["curl *"])})()
        with mock.patch.object(S, "_kind_spec", return_value=spec):
            rules = S._resolve_shell_denied("x")
        self.assertEqual(rules, ["Bash(curl *)"])
        gate = S._permission_gate(rules)
        r = asyncio.run(gate("Bash", {"command": "curl https://x"}, None))
        self.assertIsInstance(r, PermissionResultDeny)
        r = asyncio.run(gate("Bash", {"command": "ls"}, None))
        self.assertIsInstance(r, PermissionResultAllow)


class SelfTestTests(unittest.TestCase):
    def test_without_root_nothing_is_probed(self):
        with mock.patch.object(fg.os, "getuid", return_value=1000), \
                mock.patch.object(fg.subprocess, "run") as run:
            self.assertEqual(fg.self_test(60000), [])
        run.assert_not_called()

    def test_without_a_sandbox_uid_nothing_is_probed(self):
        with mock.patch.object(fg.subprocess, "run") as run:
            self.assertEqual(fg.self_test(0), [])
        run.assert_not_called()

    def test_readable_paths_are_reported_as_an_error(self):
        d = tempfile.mkdtemp()
        out = mock.Mock(stdout=f"{d}\n")
        with mock.patch.object(fg.os, "getuid", return_value=0), \
                mock.patch.object(fg.subprocess, "run", return_value=out) as run, \
                self.assertLogs(fg.LOG, "ERROR"):
            self.assertEqual(fg.self_test(60000, (d, "/non/esiste")), [d])
        self.assertEqual(run.call_args.kwargs["user"], 60000)
        self.assertNotIn("/non/esiste", run.call_args.args[0])   # missing: skipped

    def test_the_probe_reads_what_it_can(self):
        """The probe script itself, run as the current user on real paths."""
        import subprocess, sys
        d = tempfile.mkdtemp()
        r = subprocess.run([sys.executable, "-c", fg._PROBE, d, d + "/manca"],
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.split(), [d])


if __name__ == "__main__":
    unittest.main()
