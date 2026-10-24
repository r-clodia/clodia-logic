"""clodia-platform#611: cadences measured across a DST change.

Two aware datetimes with the SAME tzinfo subtract on wall-clock time: across
the switch back to standard time an hourly cron had a 0-minute gap, so its
cadence was unreadable and its misfire grace fell to the 60 s floor. The
tests pin "now" just before each switch instead of reading the real clock.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from . import scheduler


def _at(*args):
    fixed = datetime(*args, tzinfo=scheduler._SCHED_TZ)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.astimezone(tz)
    return patch.object(scheduler, "datetime", _DT)


FALL_BACK = (2026, 10, 24, 23, 0)     # 25 Oct 2026 03:00 CEST → 02:00 CET
SPRING_FWD = (2027, 3, 27, 23, 0)     # 28 Mar 2027 02:00 CET → 03:00 CEST


class AcrossTheSwitchTests(unittest.TestCase):
    def test_hourly_is_still_sixty_minutes(self):
        for when in (FALL_BACK, SPRING_FWD):
            with self.subTest(when=when), _at(*when):
                self.assertEqual(scheduler.cron_to_interval_minutes("0 * * * *"), 60)
                self.assertEqual(scheduler.min_cron_gap_minutes("0 * * * *"), 60)
                self.assertEqual(scheduler.misfire_grace_for(
                    {"cron_expr": "0 * * * *", "mode": "agentic"}), 3600)

    def test_daily_is_still_a_day(self):
        for when in (FALL_BACK, SPRING_FWD):
            with self.subTest(when=when), _at(*when):
                self.assertEqual(scheduler.cron_to_interval_minutes("0 9 * * *"), 1440)

    def test_irregular_and_yearly_still_admit_defeat(self):
        with _at(*FALL_BACK):
            self.assertIsNone(scheduler.cron_to_interval_minutes("0 9 * * 1-5"))
            self.assertIsNone(scheduler.cron_to_interval_minutes("0 9 1 1 *"))

    def test_a_dense_cron_is_still_refused_for_a_topic_trigger(self):
        with _at(*FALL_BACK):
            self.assertIsNotNone(scheduler.validate_cron_expr(
                "*/5 * * * *", min_interval_minutes=scheduler.TOPIC_TRIGGER_MIN_INTERVAL_MIN))
            self.assertIsNone(scheduler.validate_cron_expr(
                "0 * * * *", min_interval_minutes=scheduler.TOPIC_TRIGGER_MIN_INTERVAL_MIN))


NORMAL = (2026, 6, 10, 12, 0)


class IrregularStaysIrregularTests(unittest.TestCase):
    """Review of #568 (blocker): the DST fix must not let an irregular cron pass
    for a regular one — `*/45` fires at :00 and :45, gaps 15 and 45."""

    def test_irregular_sub_hour_and_hourly_crons_admit_defeat(self):
        for when in (NORMAL, FALL_BACK, SPRING_FWD):
            for expr in ("*/45 * * * *", "0,45 * * * *", "0,40 * * * *", "*/25 * * * *",
                         "*/50 * * * *", "*/7 * * * *", "*/13 * * * *",
                         "0 */5 * * *", "0 0-22 * * *"):
                with self.subTest(when=when, expr=expr), _at(*when):
                    self.assertIsNone(scheduler.cron_to_interval_minutes(expr))

    def test_regular_ones_are_still_recognised(self):
        for when in (NORMAL, FALL_BACK, SPRING_FWD):
            for expr, minutes in (("*/15 * * * *", 15), ("*/30 * * * *", 30),
                                  ("0 */2 * * *", 120), ("30 2 * * *", 1440),
                                  ("0 2 * * *", 1440)):
                with self.subTest(when=when, expr=expr), _at(*when):
                    self.assertEqual(scheduler.cron_to_interval_minutes(expr), minutes)


class TwoAmDailyTests(unittest.TestCase):
    """A daily at 02:xx really fires twice on 25 Oct, an hour apart: that is
    the switch, not its cadence (review of #568, should-fix 1)."""

    def test_its_cadence_is_a_day(self):
        for expr in ("0 2 * * *", "30 2 * * *"):
            with self.subTest(expr=expr), _at(*FALL_BACK):
                self.assertEqual(scheduler.job_cadence_minutes({"cron_expr": expr}), 1440)
                self.assertEqual(scheduler.misfire_grace_for(
                    {"cron_expr": expr, "mode": "agentic"}), 86400)

    def test_the_topic_trigger_floor_still_sees_the_real_gap(self):
        with _at(*FALL_BACK):
            self.assertEqual(scheduler.min_cron_gap_minutes("0 2 * * *"), 60)


class MissedFiresInTheRepeatedHourTests(unittest.TestCase):
    """Review of #568 (should-fix 2): at 02:30 CEST on 25 Oct the 02:00 CET fire
    is an hour AHEAD; a wall-clock comparison called it past, and the walk
    returned ten thousand copies of it."""

    JOB = {"id": "x", "cron_expr": "0 * * * *", "mode": "agentic", "enabled": True}

    def test_a_future_fire_is_not_missed(self):
        tz = scheduler._SCHED_TZ
        since = datetime(2026, 10, 25, 0, 5, tzinfo=timezone.utc)      # 02:05 CEST
        now = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).astimezone(tz)  # 02:30 CEST
        fires, capped = scheduler.missed_fires_since(self.JOB, since, now)
        self.assertEqual(fires, [])
        self.assertFalse(capped)

    def test_the_hours_around_the_switch_are_counted_once_each(self):
        tz = scheduler._SCHED_TZ
        since = datetime(2026, 10, 24, 22, 30, tzinfo=timezone.utc)    # 00:30 CEST
        now = datetime(2026, 10, 25, 2, 30, tzinfo=timezone.utc).astimezone(tz)   # 03:30 CET
        fires, capped = scheduler.missed_fires_since(self.JOB, since, now)
        self.assertFalse(capped)
        self.assertEqual([f.astimezone(timezone.utc).hour for f in fires], [23, 0, 1, 2])

    def test_catch_up_never_returns_a_future_fire(self):
        tz = scheduler._SCHED_TZ
        job = dict(self.JOB, last_run_at="2026-10-25T00:00:00+00:00")
        now = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).astimezone(tz)
        due = scheduler.catch_up_due(job, now=now)
        self.assertTrue(due is None or due.astimezone(timezone.utc) <= now.astimezone(timezone.utc))


if __name__ == "__main__":
    unittest.main()
