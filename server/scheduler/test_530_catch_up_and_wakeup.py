"""#530 — the daily digest «missed» every day.

Two causes, two guards:
1. terra is a Mac: at night OrbStack's VM is suspended, the monotonic clock
   stops, and APScheduler keeps waiting for hours that already passed on the
   wall clock. A ticker wakes it every minute so the due fire runs within its
   grace after the host wakes up.
2. a restart recomputed the next fire from «now»: the 09:00 digest was only
   recorded as `missed` by a 13:30 deploy. A fire missed while the process was
   down, still within the job's grace, is run once at boot.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from . import scheduler
from .test_boot_backfill import ORA, JobsDirTestCase


class CatchUpTests(JobsDirTestCase):
    def test_a_daily_fire_missed_this_morning_is_caught_up(self):
        # Daily at 09:00 UTC-ish cron; last run yesterday, «now» is 12:00.
        job = self._job_fermo_da(timedelta(hours=27), cron_expr="0 9 * * *",
                                 name="digest")
        due = scheduler.catch_up_due(job, now=ORA)
        self.assertIsNotNone(due)
        self.assertLessEqual((ORA - due).total_seconds(),
                             scheduler.misfire_grace_for(job))

    def test_a_fire_beyond_the_grace_is_not_caught_up(self):
        # Weekly job whose last missed fire is older than its own period? Use a
        # daily job stopped for days: only the LAST fire counts, and it is
        # within a day — so make «now» far from the last fire instead.
        job = self._job_fermo_da(timedelta(days=3), cron_expr="0 9 1 1 *",
                                 name="yearly")
        self.assertIsNone(scheduler.catch_up_due(job, now=ORA))

    def test_nothing_missed_nothing_caught_up(self):
        job = self._job_fermo_da(timedelta(minutes=5), cron_expr="0 9 * * *",
                                 name="fresh")
        self.assertIsNone(scheduler.catch_up_due(job, now=ORA))

    def test_disabled_and_topic_triggers_are_left_out(self):
        job = self._job_fermo_da(timedelta(hours=27), cron_expr="0 9 * * *",
                                 name="off")
        self.assertIsNone(scheduler.catch_up_due({**job, "enabled": False}, now=ORA))
        self.assertIsNone(scheduler.catch_up_due({**job, "mode": "topic_trigger"},
                                                 now=ORA))


class WakeupTickerTests(unittest.TestCase):
    def test_the_ticker_wakes_the_scheduler(self):
        sched = MagicMock()
        sched.running = True
        calls = []

        def fake_sleep(_s):
            calls.append(_s)
            if len(calls) > 2:
                sched.running = False

        with patch("time.sleep", fake_sleep), \
                patch("threading.Thread") as T:
            scheduler._start_wakeup_ticker(sched)
            target = T.call_args.kwargs["target"]
            self.assertTrue(T.call_args.kwargs["daemon"])
        with patch("time.sleep", fake_sleep):
            target()
        self.assertGreaterEqual(sched.wakeup.call_count, 2)
        self.assertEqual(calls[0], scheduler._WAKEUP_S)


if __name__ == "__main__":
    unittest.main()
