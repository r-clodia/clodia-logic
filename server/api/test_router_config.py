from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from . import router_config


class LiveRouterConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "catalog.yaml"
        self.live = root / "routing" / "router.yaml"
        self.live.parent.mkdir()
        self.repo.write_text(
            "recent_messages: 3\nthreshold: 0.80\nmargin: 0.015\n",
            encoding="utf-8",
        )
        self.paths = patch.multiple(
            router_config, _REPO_PATH=self.repo, _LIVE_PATH=self.live,
            _CACHE_KEY=None,
        )
        self.paths.start()
        self.addCleanup(self.paths.stop)

    def test_partial_live_override_is_merged_over_versioned_defaults(self) -> None:
        self.live.write_text("recent_messages: 5\n", encoding="utf-8")
        config = router_config.load()
        self.assertEqual(config.recent_messages, 5)
        self.assertEqual(config.threshold, 0.80)
        self.assertEqual(config.margin, 0.015)

    def test_edit_is_visible_without_restarting_or_reimporting(self) -> None:
        self.live.write_text("threshold: 0.71\n", encoding="utf-8")
        self.assertEqual(router_config.load().threshold, 0.71)
        time.sleep(0.002)
        self.live.write_text("threshold: 0.93\n", encoding="utf-8")
        self.assertEqual(router_config.load().threshold, 0.93)

    def test_invalid_live_snapshot_fails_to_safe_defaults(self) -> None:
        self.live.write_text("recent_messages: 0\nthreshold: 2\n", encoding="utf-8")
        self.assertEqual(router_config.load(), router_config.RouterConfig())


if __name__ == "__main__":
    unittest.main()
