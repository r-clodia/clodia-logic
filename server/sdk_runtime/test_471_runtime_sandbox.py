"""clodia-platform#471 — codex and opencode run under the per-spawn sandbox uid.

As root, their shell could read /proc/1/environ: the agent-server's own
environment, with the orchestrator secret that has the gateway mint any
identity. Scrubbing the inherited env (#495) closed the direct path only.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from . import session as S


class _Done(Exception):
    pass


def _ok_run(*a, **k):
    return SimpleNamespace(returncode=0, stderr=b"")


class SandboxPrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.spawn = Path(tmp.name) / "spawn"
        self.spawn.mkdir()
        self.wrapper = Path(tmp.name) / "wrap.sh"
        self.wrapper.write_text("#!/bin/sh\n")
        self.wrapper.chmod(0o755)
        for p in (patch.object(S, "_SANDBOX_UID_BASE", 20000),
                  patch.object(S, "_SANDBOX_GID_BASE", 22000),
                  patch.object(S, "_SANDBOX_KINDS", {"*"}),
                  patch.object(S, "_IS_ROOT", True),
                  patch.object(S, "_SANDBOX_WRAPPER", str(self.wrapper)),
                  patch.object(S, "_uids_in_use", set()),
                  patch("shutil.which", lambda n: f"/usr/bin/{n}")):
            p.start()
            self.addCleanup(p.stop)

    def test_off_when_the_kind_is_not_sandboxed(self) -> None:
        with patch.object(S, "_SANDBOX_KINDS", {"clodia"}):
            self.assertEqual(S._sandbox_prepare("segretario", self.spawn, "opencode"),
                             (None, [], {}))

    def test_on_returns_wrapper_uid_and_env(self) -> None:
        with patch("subprocess.run", _ok_run):
            uid, argv, env = S._sandbox_prepare(
                "fullstack-dev", self.spawn, "codex", groups=(23000,), umask="007")
        self.assertGreaterEqual(uid, 20000)
        self.assertEqual(argv, [str(self.wrapper)])
        self.assertEqual(env["CLODIA_AGENT_UID"], str(uid))
        self.assertEqual(env["CLODIA_AGENT_GID"], str(S._seed_gid("fullstack-dev")))
        self.assertEqual(env["CLODIA_REAL_CLI"], "/usr/bin/codex")
        self.assertEqual(env["HOME"], str(self.spawn))
        self.assertEqual(env["CLODIA_AGENT_GROUPS"], "23000")
        self.assertEqual(env["CLODIA_AGENT_UMASK"], "007")

    def test_fails_closed_without_the_wrapper(self) -> None:
        with patch.object(S, "_SANDBOX_WRAPPER", "/nonexistent/wrap.sh"):
            with self.assertRaises(S.SandboxUnavailable):
                S._sandbox_prepare("segretario", self.spawn, "opencode")

    def test_fails_closed_without_a_spawn(self) -> None:
        with self.assertRaises(S.SandboxUnavailable):
            S._sandbox_prepare("segretario", None, "opencode")

    def test_fails_closed_when_not_root(self) -> None:
        with patch.object(S, "_IS_ROOT", False):
            with self.assertRaises(S.SandboxUnavailable):
                S._sandbox_prepare("segretario", self.spawn, "opencode")

    def test_a_failed_chown_frees_the_uid_and_refuses(self) -> None:
        def bad(*a, **k):
            return SimpleNamespace(returncode=1, stderr=b"nope")
        with patch("subprocess.run", bad):
            with self.assertRaises(S.SandboxUnavailable):
                S._sandbox_prepare("segretario", self.spawn, "opencode")
        self.assertEqual(S._uids_in_use, set())


class CodexRunsSandboxedTests(unittest.TestCase):
    def test_codex_turn_starts_through_the_wrapper_with_the_codex_home_group(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sess = object.__new__(S.CodexChatSession)
        sess.kind = "fullstack-dev"
        sess.chat_id = "c1"
        sess._spawn = None
        sess._spawn_dir = Path(tmp.name)
        sess._codex_home = Path(tmp.name) / "codex-home"
        sess._runtime_override = {}
        sess.principal = None
        sess._sandbox_uid = None
        sess._sandbox_argv = []
        sess._sandbox_env = {}
        sess._codex_cmd = lambda model: ["codex", "exec", "-"]
        seen: dict = {}
        shared: list = []

        async def capture(*argv, **kw):
            seen["argv"], seen["env"] = list(argv), kw["env"]
            raise _Done

        prepared = (20001, ["/clodia/docker/agent-sandbox-exec.sh"],
                    {"CLODIA_AGENT_UID": "20001", "CLODIA_AGENT_GID": "22001",
                     "CLODIA_REAL_CLI": "/usr/bin/codex", "HOME": tmp.name,
                     "CLODIA_AGENT_GROUPS": str(S._CODEX_HOME_GID or 23000),
                     "CLODIA_AGENT_UMASK": "007"})
        env = {"CLODIA_ORCHESTRATOR_SECRET": "x", "LANGFUSE_SECRET_KEY": "y",
               "PATH": "/usr/bin"}
        with patch.object(S, "_sandbox_enabled", lambda k: True), \
                patch.object(S, "_sandbox_prepare", lambda *a, **k: prepared) as _p, \
                patch.object(S, "_share_codex_home", lambda h: shared.append(h)), \
                patch.object(S.pki, "mint_session_token", lambda *a, **k: "tok"), \
                patch.dict(os.environ, env, clear=True), \
                patch("asyncio.create_subprocess_exec", capture):
            with self.assertRaises(_Done):
                asyncio.run(sess._run_codex_once("hi", None))
        self.assertEqual(seen["argv"][0], "/clodia/docker/agent-sandbox-exec.sh")
        self.assertEqual(seen["argv"][1:], ["exec", "-"])
        self.assertEqual(seen["env"]["CLODIA_AGENT_UID"], "20001")
        self.assertEqual(seen["env"]["CLODIA_AGENT_UMASK"], "007")
        self.assertNotIn("CLODIA_ORCHESTRATOR_SECRET", seen["env"])
        self.assertNotIn("LANGFUSE_SECRET_KEY", seen["env"])
        self.assertEqual(shared, [sess._codex_home])     # re-shared before the turn

    def test_opencode_serve_is_started_through_the_wrapper(self) -> None:
        src = Path(S.__file__).read_text(encoding="utf-8")
        self.assertIn("self._sandbox_uid, prefix, sb_env = _sandbox_prepare(", src)
        self.assertIn('*argv, "serve", "--port"', src)


class ObservabilitySecretsTests(unittest.TestCase):
    def test_langfuse_keys_never_reach_a_spawn(self) -> None:
        with patch.dict(os.environ, {"LANGFUSE_SECRET_KEY": "s", "LANGFUSE_PUBLIC_KEY": "p",
                                     "LANGFUSE_HOST": "h", "PATH": "/usr/bin"}):
            env = S._inherited_spawn_env()
        self.assertFalse([k for k in env if k.startswith("LANGFUSE_")])

    def test_the_claude_child_env_drops_them_too(self) -> None:
        src = Path(S.__file__).read_text(encoding="utf-8")
        self.assertIn("_drop_observability_secrets(child_env)", src)


@unittest.skipUnless(os.name == "posix" and hasattr(os, "getuid") and os.getuid() == 0
                     and shutil.which("setpriv"), "needs root and setpriv")
class RealPrivilegeDropTests(unittest.TestCase):
    """Run only where it can be real (the agent-server image): through the
    actual wrapper, a process under a sandbox uid cannot open a root-only file
    — the shape of /proc/1/environ."""

    def test_the_wrapper_drops_to_a_uid_that_cannot_read_root_only_files(self) -> None:
        wrapper = Path(__file__).resolve().parents[2] / "docker" / "agent-sandbox-exec.sh"
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"orchestrator-secret")
        os.chmod(f.name, 0o400)
        self.addCleanup(os.unlink, f.name)
        env = {**os.environ, "CLODIA_AGENT_UID": "29999", "CLODIA_AGENT_GID": "29999",
               "CLODIA_REAL_CLI": "/bin/cat", "CLODIA_AGENT_UMASK": "007"}
        r = subprocess.run(["sh", str(wrapper), f.name], env=env, capture_output=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn(b"orchestrator-secret", r.stdout)
        r = subprocess.run(["sh", str(wrapper), "/proc/1/environ"], env=env,
                           capture_output=True)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()


class ReviewFollowUpTests(unittest.TestCase):
    """Follow-ups of the #496 review."""

    def test_orphans_of_a_sandbox_uid_are_killed_before_it_is_reused(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        proc = Path(tmp.name)
        mine = os.getuid()
        (proc / "4242").mkdir()                 # owned by us → "the sandbox uid"
        (proc / "self").mkdir()                 # not numeric → ignored
        killed: list = []
        with patch.object(S, "_PROC", proc), \
                patch("os.kill", lambda pid, sig: killed.append(pid)):
            S._kill_uid_processes(mine)
            S._kill_uid_processes(mine + 1)     # nobody runs as that uid
            S._kill_uid_processes(None)
        self.assertEqual(killed, [4242])

    def test_codex_home_config_is_root_owned_and_the_dir_sticky(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        (home / "auth.json").write_text("{}")
        os.chmod(home / "auth.json", 0o600)
        (home / "sessions").mkdir()
        chowned: list = []
        with patch.object(S, "_CODEX_HOME_GID", os.getgid()), \
                patch("os.chown", lambda f, u, g, **k: chowned.append((Path(f).name, u))):
            S._share_codex_home(home)
        mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)
        self.assertEqual(mode(home / "auth.json"), 0o660)          # refresh in place works
        self.assertTrue(mode(home) & stat.S_ISVTX)                 # no deleting others' files
        self.assertTrue(mode(home / "sessions") & stat.S_ISGID)
        for name in ("config.toml", "AGENTS.md"):
            self.assertEqual(mode(home / name), 0o640)             # group read-only
            self.assertIn((name, 0), chowned)                      # root-owned
        self.assertFalse(mode(home / "auth.json") & 0o007)         # no world bits

    def test_symlinks_are_not_followed_when_sharing(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "shared"
        root.mkdir()
        outside = Path(tmp.name) / "outside"
        outside.mkdir()
        os.chmod(outside, 0o700)
        (root / "planted").symlink_to(outside)
        S._group_share(root, os.getgid(), what="test")
        self.assertFalse(stat.S_IMODE(os.stat(outside).st_mode) & stat.S_ISGID)

    def test_seed_memory_behind_the_symlink_is_shared_with_the_seed_group(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mem = Path(tmp.name) / "seed-memory"
        mem.mkdir()
        (mem / "MEMORY.md").write_text("x")
        os.chmod(mem / "MEMORY.md", 0o600)
        spawn = Path(tmp.name) / "spawn"
        spawn.mkdir()
        (spawn / "memory").symlink_to(mem)
        with patch.object(S, "_seed_gid", lambda k: os.getgid()):
            S._share_seed_memory("fullstack-dev", spawn)
        self.assertEqual(stat.S_IMODE(os.stat(mem / "MEMORY.md").st_mode), 0o660)

    def test_codex_history_is_off(self) -> None:
        src = Path(S.__file__).read_text(encoding="utf-8")
        self.assertIn('[history]\\npersistence = \\"none\\"', src)
