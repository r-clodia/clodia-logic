"""#601 — the indexer's pace: pending first, then round-robin."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import topic_indexer as T

TOPICS = [{"tier": "SEAL-1", "name": n} for n in ("a", "b", "c", "d", "e")]


class PickTests(unittest.TestCase):
    def test_round_robin_covers_everything(self):
        st = {"next": 0, "pending": []}
        seen = []
        for _ in range(2):
            seen += [t["name"] for t in T.pick(TOPICS, st, n=3)]
        self.assertEqual(seen[:5], ["a", "b", "c", "d", "e"])

    def test_pending_first(self):
        st = {"next": 0, "pending": ["SEAL-1/d"]}
        self.assertEqual([t["name"] for t in T.pick(TOPICS, st, n=2)], ["d", "a"])

    def test_a_vanished_pending_topic_is_ignored(self):
        st = {"next": 0, "pending": ["SEAL-1/zz"]}
        self.assertEqual(len(T.pick(TOPICS, st, n=2)), 2)


class CycleTests(unittest.TestCase):
    def test_pending_is_remembered_and_errors_do_not_stop(self):
        tmp = Path(tempfile.mkdtemp())
        calls = []

        def sync(tier, name):
            calls.append(name)
            if name == "b":
                raise RuntimeError("HTTP 502")
            return {"pending": name == "a", "days": 1}
        with patch.object(T, "data_path", lambda n: tmp / n), \
                patch.object(T, "topics", lambda: TOPICS), \
                patch.object(T, "sync", sync), \
                patch.object(T, "PER_CYCLE", 3):
            self.assertEqual(asyncio.run(T.run_cycle()), 2)
            self.assertEqual(T._load()["pending"], ["SEAL-1/a"])
            asyncio.run(T.run_cycle())
        self.assertEqual(calls[:4], ["a", "b", "c", "a"])


if __name__ == "__main__":
    unittest.main()
