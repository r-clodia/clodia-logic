"""clodia-platform#611: cadences measured across a DST change.

Two aware datetimes with the SAME tzinfo subtract on wall-clock time: across
the switch back to standard time an hourly cron had a 0-minute gap, so its
cadence was unreadable and its misfire grace fell to the 60 s floor. The
tests pin "now" just before each switch instead of reading the real clock.
"""
from __future__ import annotations

import unittest
from datetime import datetime
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


if __name__ == "__main__":
    unittest.main()
