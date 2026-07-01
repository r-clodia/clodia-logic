import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from . import activity_log


class ActivityLogSummaryTest(TestCase):
    def test_summary_includes_seed_names_and_all_time_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "clodia"
            log_dir.mkdir(parents=True)
            (log_dir / "2026-06-30.jsonl").write_text(
                "\n".join([
                    json.dumps({"ts": "2026-06-30T10:00:00+00:00", "agent": "clodia", "type": "run_started", "payload": {}}),
                    json.dumps({"ts": "2026-06-30T10:01:00+00:00", "agent": "clodia", "type": "run_done", "payload": {"usage": {"input_tokens": 10, "output_tokens": 4}}}),
                ]) + "\n",
                encoding="utf-8",
            )
            (log_dir / "2026-07-01.jsonl").write_text(
                "\n".join([
                    json.dumps({"ts": "2026-07-01T11:00:00+00:00", "agent": "clodia", "type": "run_started", "payload": {}}),
                    json.dumps({"ts": "2026-07-01T11:01:00+00:00", "agent": "clodia", "type": "run_done", "payload": {"usage": {"input_tokens": 7, "output_tokens": 3}}}),
                ]) + "\n",
                encoding="utf-8",
            )

            with patch.object(activity_log, "ACTIVITY_DIR", root):
                rows = {r["agent"]: r for r in activity_log.summary(["clodia", "wainston"])}

        self.assertEqual(rows["clodia"]["runs"], 2)
        self.assertEqual(rows["clodia"]["tokens_in"], 17)
        self.assertEqual(rows["clodia"]["tokens_out"], 7)
        self.assertEqual(rows["wainston"]["runs"], 0)
        self.assertEqual(rows["wainston"]["tokens_in"], 0)
        self.assertEqual(rows["wainston"]["tokens_out"], 0)
