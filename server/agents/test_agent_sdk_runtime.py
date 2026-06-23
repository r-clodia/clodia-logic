from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from .models import AgentSpec
from . import workspace as workspace_mod


def _agent_dir(root: Path) -> Path:
    agent = root / "saim"
    agent.mkdir()
    (agent / "system-prompt.md").write_text("Sei Saim.")
    (agent / "memory").mkdir()
    return agent


class AgentSdkRuntimeTests(unittest.TestCase):
    def test_legacy_agent_defaults_to_claude(self) -> None:
        spec = AgentSpec.model_validate({
            "name": "ada",
            "description": "dev",
            "model": "claude-opus-4-7",
            "display_name": "Ada",
            "system_prompt": "system-prompt.md",
        })
        self.assertEqual(spec.agent_sdk, "claude")

    def test_codex_workspace_gets_neutral_and_runtime_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_root = workspace_mod.WORKSPACES_ROOT
            workspace_mod.WORKSPACES_ROOT = root / "workspaces"
            try:
                agent = _agent_dir(root)
                spec = AgentSpec.model_validate({
                    "name": "saim",
                    "description": "codex agent",
                    "agent_sdk": "codex",
                    "model": "gpt-5.5",
                    "display_name": "Saim",
                    "sandbox": {
                        "allow_read": ["{scratch}/**"],
                        "allow_write": ["{scratch}/**"],
                    },
                    "capabilities": ["article-spec"],
                    "memory": {"dir": "memory/"},
                    "system_prompt": "system-prompt.md",
                })
                spec.agent_dir = str(agent)

                ws = workspace_mod.EphemeralWorkspace(spec, task_id="test")
                ws.create()
                try:
                    self.assertTrue((ws.dir / ".agent" / "skills" / "article-spec" / "SKILL.md").is_file())
                    self.assertTrue((ws.dir / "AGENTS.md").is_file())
                    self.assertFalse((ws.dir / ".claude").exists())
                finally:
                    ws.cleanup()
            finally:
                workspace_mod.WORKSPACES_ROOT = old_root


if __name__ == "__main__":
    unittest.main()
