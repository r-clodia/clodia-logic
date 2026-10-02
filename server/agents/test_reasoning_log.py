"""Stored turn reasoning: where it is written, how long it is kept, what goes.

clodia-platform#484. A turn's reasoning used to be live-only: whoever reopened
the topic after the turn had no way to see it. These tests cover the store —
not the emission pipeline, which lives in `sdk_runtime`.

The first property is CONTAINMENT, not convenience: the existing activity log
is indexed by agent and **does not know which tier the event happened in**
(`agent-state/activity/<agent>/YYYY-MM-DD.jsonl`). The reasoning of a SEAL-4
turn and that of a SEAL-0 turn would end up in the same file, and reasoning
quotes the channel's content. That is why the store is separate and the first
thing its path names is the tier.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from . import reasoning_log, transcript_retention


class _WithStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "reasoning"
        self._p = patch.object(reasoning_log, "REASONING_DIR", self.root)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(self._tmp.cleanup)
        # A clean retention policy: no leftovers from the environment.
        self._env = patch.dict(os.environ, {"CLODIA_TRANSCRIPT_RETENTION": "",
                                            "CLODIA_TRANSCRIPT_RETENTION_OTHER": ""})
        self._env.start()
        self.addCleanup(self._env.stop)


class ThePathNamesTheTier(_WithStore):
    """Containment lives in the structure, not in a field inside the file."""

    def test_the_tier_is_the_first_directory(self) -> None:
        reasoning_log.record("SEAL-2", "preventivi-tomato", message_id="m1",
                             spawn="clodia-7", seed="clodia", text="thinking")
        written = sorted(p.relative_to(self.root).as_posix()
                         for p in self.root.rglob("*.jsonl"))
        self.assertEqual(1, len(written))
        self.assertTrue(written[0].startswith("SEAL-2/preventivi-tomato/"),
                        f"the path does not start with the tier: {written[0]}")

    def test_two_tiers_share_no_file(self) -> None:
        """The defect being avoided: one single file per agent."""
        reasoning_log.record("SEAL-0", "public", message_id="a",
                             spawn="clodia-1", seed="clodia", text="trivial")
        reasoning_log.record("SEAL-4", "restricted", message_id="b",
                             spawn="clodia-2", seed="clodia", text="sensitive")
        for p in self.root.rglob("*.jsonl"):
            body = p.read_text(encoding="utf-8")
            self.assertFalse("trivial" in body and "sensitive" in body,
                             "two tiers in the same file")

    def test_a_name_escaping_the_directory_is_rejected(self) -> None:
        for tier, name in (("../etc", "x"), ("SEAL-1", "../../outside"),
                           ("SEAL-1", "a/b"), ("", "x")):
            with self.subTest(tier=tier, name=name):
                with self.assertRaises(ValueError):
                    reasoning_log.record(tier, name, message_id="m",
                                         spawn="s", seed="s", text="t")


class Permissions(_WithStore):
    """Sandboxed agent uids rely on permissions: 0o700 dirs, 0o600 files."""

    def test_directories_are_0700_and_files_0600(self) -> None:
        old = os.umask(0o022)
        try:
            reasoning_log.record("SEAL-2", "c", message_id="m", spawn="s",
                                 seed="s", text="private")
        finally:
            os.umask(old)
        for d in (self.root, self.root / "SEAL-2", self.root / "SEAL-2" / "c"):
            self.assertEqual(0o700, stat.S_IMODE(d.stat().st_mode), d)
        f = next(self.root.rglob("*.jsonl"))
        self.assertEqual(0o600, stat.S_IMODE(f.stat().st_mode))

    def test_appending_keeps_the_file_private(self) -> None:
        for mid in ("m1", "m2"):
            reasoning_log.record("SEAL-1", "c", message_id=mid, spawn="s",
                                 seed="s", text="t")
        f = next(self.root.rglob("*.jsonl"))
        self.assertEqual(0o600, stat.S_IMODE(f.stat().st_mode))
        self.assertEqual(["m1", "m2"], reasoning_log.index("SEAL-1", "c"))


class ThePerTurnCap(_WithStore):
    """64 KB per turn: 32k head and 32k tail.

    Reasoning is verbose and the cap is needed, but cutting ONLY the tail
    would drop the conclusion — the part one comes back to read — and cutting
    only the head would drop how the problem was framed. Both ends are kept
    and the omitted middle is stated.
    """

    def test_below_the_cap_the_text_is_intact(self) -> None:
        text = "x" * 1000
        self.assertEqual((text, False), reasoning_log.cap(text))

    def test_exactly_at_the_cap_nothing_is_cut(self) -> None:
        text = "x" * (reasoning_log.HEAD_CHARS + reasoning_log.TAIL_CHARS)
        self.assertEqual((text, False), reasoning_log.cap(text))

    def test_above_the_cap_both_ends_remain(self) -> None:
        text = ("A" * reasoning_log.HEAD_CHARS + "M" * 5000
                + "Z" * reasoning_log.TAIL_CHARS)
        cut, truncated = reasoning_log.cap(text)
        self.assertTrue(truncated)
        self.assertTrue(cut.startswith("A" * 100))
        self.assertTrue(cut.endswith("Z" * 100))
        self.assertNotIn("M", cut, "the omitted part is the MIDDLE")
        self.assertIn("5000", cut, "the cut must be stated, not hidden")

    def test_the_written_record_respects_the_cap(self) -> None:
        reasoning_log.record("SEAL-1", "c", message_id="m", spawn="s", seed="s",
                             text="A" * 200_000)
        entry = reasoning_log.read("SEAL-1", "c", "m")
        self.assertTrue(entry["truncated"])
        self.assertLessEqual(
            len(entry["text"]),
            reasoning_log.HEAD_CHARS + reasoning_log.TAIL_CHARS + 200,
            "the cap does not apply to what lands on disk")


class ReadBackPerMessage(_WithStore):
    """The key is the MESSAGE id, not the turn: the bubble is what one looks at."""

    def test_write_and_read_back(self) -> None:
        reasoning_log.record("SEAL-1", "c", message_id="m1", spawn="clodia-7",
                             seed="clodia", text="think first, then write")
        entry = reasoning_log.read("SEAL-1", "c", "m1")
        self.assertEqual("think first, then write", entry["text"])
        self.assertEqual("clodia-7", entry["spawn"])
        self.assertEqual("m1", entry["message_id"])

    def test_a_message_without_reasoning_does_not_exist(self) -> None:
        self.assertIsNone(reasoning_log.read("SEAL-1", "c", "never-seen"))

    def test_the_index_lists_only_messages_that_have_something(self) -> None:
        """No ghost bubbles: the index lights the 💭 button in the UI, and a
        button that opens nothing is worse than no button."""
        reasoning_log.record("SEAL-1", "c", message_id="m1", spawn="s",
                             seed="s", text="something")
        self.assertEqual(["m1"], reasoning_log.index("SEAL-1", "c"))

    def test_empty_text_is_not_recorded(self) -> None:
        reasoning_log.record("SEAL-1", "c", message_id="m1", spawn="s",
                             seed="s", text="   \n ")
        self.assertEqual([], reasoning_log.index("SEAL-1", "c"))

    def test_one_channel_index_does_not_see_another(self) -> None:
        reasoning_log.record("SEAL-1", "one", message_id="m1", spawn="s",
                             seed="s", text="t")
        reasoning_log.record("SEAL-1", "two", message_id="m2", spawn="s",
                             seed="s", text="t")
        self.assertEqual(["m1"], reasoning_log.index("SEAL-1", "one"))
        self.assertIsNone(reasoning_log.read("SEAL-1", "one", "m2"))

    def test_a_corrupt_line_does_not_break_reading(self) -> None:
        reasoning_log.record("SEAL-1", "c", message_id="m1", spawn="s",
                             seed="s", text="good")
        f = next(self.root.rglob("*.jsonl"))
        with f.open("a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        self.assertEqual(["m1"], reasoning_log.index("SEAL-1", "c"))


class Retention(_WithStore):
    """Per tier: min(tier transcript retention, 90 days). Reasoning is the
    bulkiest data the platform writes per turn: without an expiry the store
    grows forever — and it must never outlive the transcripts of its tier."""

    def _old_file(self, tier: str, days: int) -> Path:
        when = datetime.now(timezone.utc) - timedelta(days=days)
        p = self.root / tier / "c" / f"{when.strftime('%Y-%m-%d')}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"message_id": "old", "text": "t"}) + "\n",
                     encoding="utf-8")
        return p

    def test_past_90_days_it_goes(self) -> None:
        old = self._old_file("SEAL-1", 91)
        recent = self._old_file("SEAL-1", 89)
        self.assertEqual({"SEAL-1": 1}, reasoning_log.purge())
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_the_expiry_applies_in_every_tier(self) -> None:
        old = [self._old_file(t, 200) for t in ("SEAL-0", "SEAL-3")]
        reasoning_log.purge()
        for v in old:
            self.assertFalse(v.exists(), f"{v} survived the expiry")

    def test_a_tier_with_shorter_transcript_retention_prunes_sooner(self) -> None:
        """#446: a SEAL-2 topic kept 30 days must not keep its reasoning 90."""
        seal2 = self._old_file("SEAL-2", 40)
        seal1 = self._old_file("SEAL-1", 40)
        with patch.dict(os.environ, {"CLODIA_TRANSCRIPT_RETENTION": "SEAL-2=30"}):
            self.assertEqual(30, reasoning_log.policy()["SEAL-2"])
            removed = reasoning_log.purge()
        self.assertEqual({"SEAL-2": 1}, removed)
        self.assertFalse(seal2.exists())
        self.assertTrue(seal1.exists())

    def test_longer_or_unlimited_transcript_retention_is_capped_at_90(self) -> None:
        with patch.dict(os.environ,
                        {"CLODIA_TRANSCRIPT_RETENTION": "SEAL-0=0,SEAL-1=365"}):
            pol = reasoning_log.policy()
        self.assertEqual(reasoning_log.MAX_DAYS, pol["SEAL-0"])
        self.assertEqual(reasoning_log.MAX_DAYS, pol["SEAL-1"])

    def test_an_undated_file_name_stays(self) -> None:
        """What cannot be dated is not deleted: that is how a cleanup turns
        into a loss."""
        odd = self.root / "SEAL-1" / "c" / "notes.jsonl"
        odd.parent.mkdir(parents=True, exist_ok=True)
        odd.write_text("{}\n", encoding="utf-8")
        reasoning_log.purge()
        self.assertTrue(odd.exists())


class PruningRunsInTheRetentionLoop(_WithStore):
    """The pruning runs in `transcript_retention.run_once` (the retention
    loop), with no new writes needed, and lands on the audit trail."""

    def _run_once(self) -> AsyncMock:
        report = AsyncMock(return_value=True)
        with tempfile.TemporaryDirectory() as t, \
                patch.object(transcript_retention, "TRANSCRIPTS_DIR", Path(t)), \
                patch("server.audit_events.report", report):
            asyncio.run(transcript_retention.run_once())
        return report

    def test_prunes_without_any_new_write_and_reports_it(self) -> None:
        when = datetime.now(timezone.utc) - timedelta(days=40)
        old = self.root / "SEAL-2" / "c" / f"{when.strftime('%Y-%m-%d')}.jsonl"
        old.parent.mkdir(parents=True)
        old.write_text("{}\n", encoding="utf-8")
        with patch.dict(os.environ, {"CLODIA_TRANSCRIPT_RETENTION": "SEAL-2=30"}):
            report = self._run_once()
        self.assertFalse(old.exists(), "pruned only on writes")
        events = [c.args[0] for c in report.await_args_list]
        reasoning = [e for e in events if e.get("resource") == "reasoning"]
        self.assertEqual(1, len(reasoning), events)
        ev = reasoning[0]
        self.assertEqual("control.retention", ev["type"])
        self.assertEqual("apply", ev["action"])
        self.assertEqual({"SEAL-2": 1}, ev["result"]["removed"])
        self.assertEqual(30, ev["result"]["policy"]["SEAL-2"])
        self.assertIn("transcripts", [e.get("resource") for e in events],
                      "the transcript pruning must still run")

    def test_a_failing_reasoning_pruning_does_not_stop_the_loop(self) -> None:
        with patch.object(reasoning_log, "purge", side_effect=OSError("disk")):
            report = self._run_once()
        self.assertEqual(["transcripts"],
                         [c.args[0]["resource"] for c in report.await_args_list])

    def test_a_write_does_not_prune_by_itself(self) -> None:
        """Every pruning is on the trail: the write path no longer deletes."""
        when = datetime.now(timezone.utc) - timedelta(days=400)
        old = self.root / "SEAL-1" / "c" / f"{when.strftime('%Y-%m-%d')}.jsonl"
        old.parent.mkdir(parents=True)
        old.write_text("{}\n", encoding="utf-8")
        reasoning_log.record("SEAL-1", "c", message_id="m", spawn="s",
                             seed="s", text="today")
        self.assertTrue(old.exists())


class NoLongerInTheActivityLog(unittest.TestCase):
    """The activity log docstring used to promise `thinking_chunk` among its
    future extensions. That was the wrong road, and the reason this issue
    could have been implemented badly: that file is indexed by AGENT and does
    not know which tier it is writing in."""

    def test_the_activity_log_no_longer_promises_reasoning(self) -> None:
        from . import activity_log
        doc = activity_log.__doc__ or ""
        self.assertNotIn("Future estensioni", doc,
                         "reasoning is not a future extension of this file: "
                         "it is a closed road, and that must be said")
        self.assertIn("reasoning_log", doc,
                      "a reader here must find where reasoning went")
        self.assertIn("tier", doc, "it must say WHY it is not here")


if __name__ == "__main__":
    unittest.main()
