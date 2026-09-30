"""clodia-platform#471 — codex and opencode spawns must not inherit the
orchestrator-only secrets or the container's provider keys."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from . import session


SENSITIVE = {"CLODIA_ORCHESTRATOR_SECRET": "orch", "GIT_TOKEN": "ghp_x",
             "ANTHROPIC_API_KEY": "sk-ant-x", "OPENAI_API_KEY": "sk-x"}


class InheritedSpawnEnvTests(unittest.TestCase):
    def test_orchestrator_secrets_and_provider_keys_are_dropped(self) -> None:
        with patch.dict(os.environ, {**SENSITIVE, "PATH": "/usr/bin", "HOME": "/root"}):
            env = session._inherited_spawn_env()
        for k in SENSITIVE:
            self.assertNotIn(k, env)
        self.assertEqual("/usr/bin", env["PATH"])
        self.assertEqual("/root", env["HOME"])

    def test_the_process_env_itself_is_not_touched(self) -> None:
        with patch.dict(os.environ, SENSITIVE):
            session._inherited_spawn_env()
            self.assertEqual("orch", os.environ["CLODIA_ORCHESTRATOR_SECRET"])

    def test_both_runtimes_build_their_env_from_it(self) -> None:
        src = open(session.__file__, encoding="utf-8").read()
        self.assertIn('env = {**_inherited_spawn_env(), "CODEX_HOME"', src)   # codex
        self.assertIn("        env = _inherited_spawn_env()\n", src)            # opencode
        self.assertNotIn('env = {**os.environ, "CODEX_HOME"', src)
        self.assertNotIn("        env = {**os.environ}\n", src)


if __name__ == "__main__":
    unittest.main()
