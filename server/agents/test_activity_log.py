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


class ProviderSummaryTest(TestCase):
    def test_provider_from_event_and_unknown_bucket_no_guessing(self):
        """Il provider viene dal payload; gli eventi SENZA provider finiscono in
        'sconosciuto' — NON si indovina il provider corrente (bug mis-attribuzione)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "clodia"
            log_dir.mkdir(parents=True)
            (log_dir / "2026-07-01.jsonl").write_text(
                "\n".join([
                    # con provider esplicito
                    json.dumps({"ts": "2026-07-01T10:00:00+00:00", "type": "run_done",
                                "payload": {"provider": "claude-pro-max",
                                            "usage": {"input_tokens": 100, "output_tokens": 10}}}),
                    # storico SENZA provider → sconosciuto (niente fallback al corrente)
                    json.dumps({"ts": "2026-07-01T10:05:00+00:00", "type": "run_done",
                                "payload": {"usage": {"input_tokens": 999, "output_tokens": 1}}}),
                ]) + "\n",
                encoding="utf-8",
            )
            with patch.object(activity_log, "ACTIVITY_DIR", root):
                rows = {r["provider"]: r for r in activity_log.provider_summary(["clodia"])}

        self.assertEqual(rows["claude-pro-max"]["tokens_in"], 100)
        self.assertEqual(rows["claude-pro-max"]["tokens_out"], 10)
        self.assertIn("sconosciuto", rows)   # lo storico NON è attribuito a un provider reale
        self.assertEqual(rows["sconosciuto"]["tokens_in"], 999)


class CodexUsageDeltaTest(TestCase):
    def test_cumulative_thread_usage_becomes_per_run_delta(self):
        """Codex riporta il cumulativo del thread → run_done deve registrare il
        delta di questo run, così la somma dei run non multi-conta."""
        import types
        from ..sdk_runtime.session import CodexChatSession
        fn = CodexChatSession._codex_run_usage_delta
        s = types.SimpleNamespace()
        d1 = fn(s, {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 50})
        self.assertEqual(d1, {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 50})
        d2 = fn(s, {"input_tokens": 250, "output_tokens": 25, "cache_read_input_tokens": 180})
        self.assertEqual(d2, {"input_tokens": 150, "output_tokens": 15, "cache_read_input_tokens": 130})
        # thread ripartito (cumulativo cala) → il run riparte da 0
        d3 = fn(s, {"input_tokens": 30, "output_tokens": 3, "cache_read_input_tokens": 10})
        self.assertEqual(d3, {"input_tokens": 30, "output_tokens": 3, "cache_read_input_tokens": 10})
        # usage vuoto / assente → None (nessun turn.completed)
        self.assertIsNone(fn(s, None))
        self.assertIsNone(fn(s, {}))
