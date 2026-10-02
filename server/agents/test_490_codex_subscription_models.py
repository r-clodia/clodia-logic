"""clodia-platform#490 — a seed on the ChatGPT subscription must declare a model
the subscription accepts.

The subscription (`providers/codex.yaml`, `models_excluded: ["*-codex*"]`)
answers 400 to the `-codex` family. A codex seed with such a model was routed
to the paid `openai-api` provider, while the codex runtime kept the ChatGPT
login: every turn failed. The owner wants the subscription, not pay-per-use.
"""
from __future__ import annotations

import fnmatch
import unittest
from pathlib import Path

import yaml

from ..config import workspace_path

AGENTS = Path(workspace_path("catalogs/packs/base-pack/agents"))
EXCLUDED = yaml.safe_load(Path(workspace_path("providers/codex.yaml")).read_text(
    encoding="utf-8")).get("models_excluded") or []


def _seeds():
    for d in sorted(AGENTS.iterdir()):
        f = d / "agent.yaml"
        if f.is_file():
            yield d.name, yaml.safe_load(f.read_text(encoding="utf-8")) or {}


class CodexSubscriptionModelTests(unittest.TestCase):
    def test_no_subscription_seed_declares_an_excluded_model(self) -> None:
        for name, s in _seeds():
            if s.get("agent_sdk") != "codex":
                continue
            providers = s.get("providers")
            on_subscription = providers is None or "codex" in providers
            models = [s.get("model")] + [st.get("model") for st in (s.get("stacks") or [])
                                         if st.get("provider") in (None, "codex")]
            for m in filter(None, models):
                with self.subTest(seed=name, model=m):
                    if on_subscription:
                        self.assertFalse(
                            any(fnmatch.fnmatch(m, p) for p in EXCLUDED),
                            f"{name} is on the ChatGPT subscription with {m}, "
                            "which the subscription rejects (400)")

    def test_ophelia_is_pinned_to_the_subscription(self) -> None:
        s = dict(_seeds())["ophelia"]
        self.assertEqual(s.get("providers"), ["codex"])
        self.assertFalse(any(fnmatch.fnmatch(s["model"], p) for p in EXCLUDED))


if __name__ == "__main__":
    unittest.main()
