"""#711 — after a host wake every missed job fired in the same second.

The pacer releases catch-ups one at a time, spaced and capped, programmatic
before agentic, one per job, within the grace, and records every fire it does
not run. A punctual fire outside a wake window is untouched.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from . import db, scheduler
from .catch_up import CatchUpEntry, CatchUpPacer, CatchUpSettings, WakeDetector
from .test_boot_backfill import JobsDirTestCase

T0 = datetime(2026, 11, 21, 8, 54, tzinfo=timezone.utc)


class FakeClock:
    """Wall clock that only moves when someone sleeps on it."""

    def __init__(self, start: datetime = T0) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)


def _entry(job_id: int, *, late_min: float = 94, grace_s: float = 86400,
           programmatic: bool = False, now: datetime = T0) -> CatchUpEntry:
    scheduled = now - timedelta(minutes=late_min)
    return CatchUpEntry(job_id=job_id, scheduled=scheduled,
                        deadline=scheduled + timedelta(seconds=grace_s),
                        programmatic=programmatic, reason="test")


class PacerTests(unittest.TestCase):
    def _pacer(self, clock, *, runner=None, drops=None, **kw):
        settings = CatchUpSettings(**{"spacing_s": 60.0, "jitter_s": 60.0,
                                      "max_concurrent": 1, **kw})
        started: list[tuple[int, datetime]] = []

        async def record(job_id):
            started.append((job_id, clock.now()))

        pacer = CatchUpPacer(
            runner or record, settings=settings, now=clock.now,
            sleep=clock.sleep, rand=lambda: 0.5,
            on_drop=(lambda e, why: drops.append((e, why))) if drops is not None
            else None)
        return pacer, started

    def test_a_clock_jump_with_several_missed_jobs_is_spaced(self):
        """Five jobs missed during a 1.6 h sleep: five releases, not one burst."""
        clock = FakeClock()

        async def scenario():
            pacer, started = self._pacer(clock)
            for jid in (1, 2, 3, 4, 5):
                self.assertEqual(pacer.offer(_entry(jid)), "queued")
            await pacer.drain()
            return pacer, started

        pacer, started = asyncio.run(scenario())
        self.assertEqual(len(started), 5)
        self.assertEqual(started[0][1], T0, "the first catch-up is not delayed")
        gaps = [(b[1] - a[1]).total_seconds() for a, b in zip(started, started[1:])]
        # spacing 60 + jitter 60 × rand 0.5 = 90 s between every release
        self.assertEqual(gaps, [90.0] * 4)
        self.assertFalse(pacer.busy())

    def test_the_gap_stays_within_spacing_and_jitter(self):
        clock = FakeClock()
        draws = iter([0.0, 1.0, 0.3, 0.9, 0.5])

        async def scenario():
            started = []

            async def record(job_id):
                started.append(clock.now())

            pacer = CatchUpPacer(record, settings=CatchUpSettings(
                spacing_s=60, jitter_s=60, max_concurrent=1),
                now=clock.now, sleep=clock.sleep, rand=lambda: next(draws))
            for jid in range(1, 6):
                pacer.offer(_entry(jid))
            await pacer.drain()
            return started

        started = asyncio.run(scenario())
        gaps = [(b - a).total_seconds() for a, b in zip(started, started[1:])]
        self.assertEqual(gaps, [60.0, 120.0, 78.0, 114.0])

    def test_programmatic_jobs_go_before_agentic_ones(self):
        clock = FakeClock()

        async def scenario():
            pacer, started = self._pacer(clock)
            pacer.offer(_entry(10))
            pacer.offer(_entry(11, programmatic=True))
            pacer.offer(_entry(12))
            pacer.offer(_entry(13, programmatic=True))
            await pacer.drain()
            return [j for j, _ in started]

        # The four arrive in the same burst: logic jobs first, then agentic,
        # each class by deadline then by fire.
        self.assertEqual(asyncio.run(scenario()), [11, 13, 10, 12])

    def test_concurrency_is_capped(self):
        clock = FakeClock()

        async def scenario():
            gates = {j: asyncio.Event() for j in range(1, 5)}
            running: set[int] = set()
            peak = [0]

            async def hold(job_id):
                running.add(job_id)
                peak[0] = max(peak[0], len(running))
                await gates[job_id].wait()
                running.discard(job_id)

            pacer, _ = self._pacer(clock, runner=hold, max_concurrent=2,
                                   spacing_s=0, jitter_s=0)
            for j in gates:
                pacer.offer(_entry(j))
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertEqual(sorted(running), [1, 2])
            self.assertEqual(pacer.pending(), [3, 4])
            gates[1].set()
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertEqual(sorted(running), [2, 3])
            for g in gates.values():
                g.set()
            await pacer.drain()
            return peak[0]

        self.assertEqual(asyncio.run(scenario()), 2)

    def test_a_job_keeps_one_catch_up_and_the_extra_fire_is_reported(self):
        clock = FakeClock()
        drops: list = []

        async def scenario():
            gate = asyncio.Event()

            async def hold(job_id):
                await gate.wait()

            pacer, _ = self._pacer(clock, runner=hold, drops=drops)
            pacer.offer(_entry(1))                      # released, in flight
            for _ in range(5):
                await asyncio.sleep(0)
            pacer.offer(_entry(2, late_min=120))        # queued
            self.assertEqual(pacer.offer(_entry(2, late_min=60)), "coalesced")
            self.assertEqual(pacer.pending(), [2])
            kept = pacer._queue[2]
            self.assertEqual(kept.scheduled, T0 - timedelta(minutes=60),
                             "the most recent fire is the one kept")
            self.assertEqual(kept.fires, 2)
            # a fire of a job whose catch-up is already running is collapsed too
            self.assertEqual(pacer.offer(_entry(1, late_min=1)), "coalesced")
            gate.set()
            await pacer.drain()

        asyncio.run(scenario())
        self.assertEqual(len(drops), 2)
        (first, why1), (second, why2) = drops
        self.assertEqual(first.job_id, 2)
        self.assertEqual(first.scheduled, T0 - timedelta(minutes=120))
        self.assertIn("collapsed into the pending catch-up", why1)
        self.assertEqual(second.job_id, 1)
        self.assertIn("already running", why2)

    def test_the_grace_is_honoured_and_the_expiry_is_reported(self):
        """A topic trigger has a 60 s grace: behind two other catch-ups it
        expires, and the pacer says so instead of running it late or dropping
        it in silence."""
        clock = FakeClock()
        drops: list = []

        async def scenario():
            pacer, started = self._pacer(clock, drops=drops)
            pacer.offer(_entry(1, programmatic=True))
            pacer.offer(_entry(2, programmatic=True))
            pacer.offer(_entry(3, late_min=0, grace_s=60))
            await pacer.drain()
            return [j for j, _ in started]

        self.assertEqual(asyncio.run(scenario()), [1, 2])
        (entry, why), = drops
        self.assertEqual(entry.job_id, 3)
        self.assertIn("past its grace", why)

    def test_a_failing_run_frees_its_slot(self):
        clock = FakeClock()

        async def scenario():
            seen = []

            async def boom(job_id):
                seen.append(job_id)
                raise RuntimeError("gateway down")

            pacer, _ = self._pacer(clock, runner=boom)
            pacer.offer(_entry(1))
            pacer.offer(_entry(2))
            await pacer.drain()
            return seen

        self.assertEqual(asyncio.run(scenario()), [1, 2])


class WakeDetectorTests(unittest.TestCase):
    def test_a_wall_clock_jump_opens_the_window(self):
        clocks = {"wall": 1000.0, "mono": 50.0}
        det = WakeDetector(CatchUpSettings(wall_jump_s=120, window_s=600),
                           wall=lambda: clocks["wall"], mono=lambda: clocks["mono"])
        self.assertIsNone(det.tick())
        clocks["wall"] += 60
        clocks["mono"] += 60
        self.assertIsNone(det.tick(), "a regular tick is not a wake")
        self.assertFalse(det.active())
        # 1.6 h asleep: the wall clock moved, the monotonic one did not
        clocks["wall"] += 60 + 5760
        clocks["mono"] += 60
        self.assertAlmostEqual(det.tick(), 5760)
        self.assertTrue(det.active())
        clocks["mono"] += 601
        clocks["wall"] += 601
        self.assertFalse(det.active(), "the window closes")


class SettingsTests(unittest.TestCase):
    def test_env_overrides_and_garbage_falls_back(self):
        env = {"CLODIA_CATCHUP_SPACING_S": "120", "CLODIA_CATCHUP_JITTER_S": "x",
               "CLODIA_CATCHUP_MAX_CONCURRENT": "0"}
        with patch.dict("os.environ", env):
            s = CatchUpSettings.from_env()
        self.assertEqual(s.spacing_s, 120.0)
        self.assertEqual(s.jitter_s, CatchUpSettings().jitter_s)
        self.assertEqual(s.max_concurrent, 1, "never zero: nothing would run")


class SchedulerRoutingTests(JobsDirTestCase):
    """The decision in `_fire_job_threadsafe`: catch-up or normal cadence."""

    def setUp(self):
        super().setUp()
        scheduler._BOOT_CATCH_UP.clear()
        self._wake_patch = patch.object(scheduler, "_wake", None)
        self._wake_patch.start()

    def tearDown(self):
        self._wake_patch.stop()
        scheduler._BOOT_CATCH_UP.clear()
        super().tearDown()

    def _job(self, **kw):
        return db.create_job(kw.pop("name", "digest"), kw.pop("cron_expr", "0 9 * * *"),
                             "prompt", **kw)

    def test_a_punctual_fire_is_not_touched(self):
        job = self._job()
        now = datetime.now(timezone.utc)
        self.assertIsNone(scheduler._catch_up_entry(job["id"], now - timedelta(seconds=2)))
        self.assertIsNone(scheduler._catch_up_entry(job["id"], None))

    def test_a_late_fire_is_a_catch_up_with_the_job_grace(self):
        job = self._job()
        due = datetime.now(timezone.utc) - timedelta(minutes=94)
        e = scheduler._catch_up_entry(job["id"], due)
        self.assertIsNotNone(e)
        self.assertEqual(e.scheduled, due)
        self.assertEqual(e.deadline - due,
                         timedelta(seconds=scheduler.misfire_grace_for(job)))
        self.assertFalse(e.programmatic)

    def test_logic_jobs_are_programmatic(self):
        job = self._job(name="logic", mode="logic",
                        plan=[{"verb": "x.y", "args": {}}])
        e = scheduler._catch_up_entry(
            job["id"], datetime.now(timezone.utc) - timedelta(minutes=30))
        self.assertTrue(e.programmatic)

    def test_a_boot_recovery_is_a_catch_up_once(self):
        job = self._job()
        recovered = datetime.now(timezone.utc) - timedelta(hours=3)
        scheduler._BOOT_CATCH_UP[job["id"]] = recovered
        e = scheduler._catch_up_entry(job["id"], datetime.now(timezone.utc))
        self.assertEqual(e.scheduled, recovered)
        self.assertIsNone(scheduler._catch_up_entry(job["id"], datetime.now(timezone.utc)),
                          "the next regular fire is normal cadence again")

    def test_inside_the_wake_window_even_a_punctual_fire_is_staggered(self):
        job = self._job()
        det = WakeDetector(CatchUpSettings(), wall=lambda: 0.0, mono=lambda: 0.0)
        det.enter()
        with patch.object(scheduler, "_wake", det):
            e = scheduler._catch_up_entry(job["id"], datetime.now(timezone.utc))
        self.assertIsNotNone(e)

    def test_a_dropped_catch_up_leaves_a_missed_row(self):
        job = self._job()
        scheduler._record_catch_up_drop(_entry(job["id"]), "past its grace")
        row = db.get_job(job["id"])["runs"][-1]
        self.assertEqual(row["stato"], db.MISSED)
        self.assertIn("past its grace", row["note"])


class EndToEndTests(JobsDirTestCase):
    """APScheduler's thread hands three late fires and one punctual one to
    `_fire_job_threadsafe`: the punctual fire runs at once, the late ones go
    one at a time, spaced."""

    def setUp(self):
        super().setUp()
        scheduler._BOOT_CATCH_UP.clear()
        with scheduler._DUE_LOCK:
            scheduler._DUE.clear()

    def tearDown(self):
        scheduler._BOOT_CATCH_UP.clear()
        super().tearDown()

    def test_late_fires_spaced_punctual_fire_immediate(self):
        jobs = [db.create_job(f"j{i}", "0 9 * * *", "p") for i in range(4)]
        clock = FakeClock(datetime.now(timezone.utc))
        fired: list[tuple[int, datetime]] = []

        async def fake_fire(job_id):
            fired.append((job_id, clock.now()))
            return {"status": "ok"}

        loop = asyncio.new_event_loop()
        pacer = CatchUpPacer(scheduler._run_catch_up,
                             settings=CatchUpSettings(spacing_s=90, jitter_s=0),
                             now=clock.now, sleep=clock.sleep)
        try:
            with patch.object(scheduler, "_loop", loop), \
                    patch.object(scheduler, "_pacer", pacer), \
                    patch.object(scheduler, "_wake", None), \
                    patch.object(scheduler, "fire_job", fake_fire), \
                    patch.object(scheduler, "_catch_up_settings", CatchUpSettings()):
                late = datetime.now(timezone.utc) - timedelta(minutes=94)
                for j in jobs[:3]:
                    with scheduler._DUE_LOCK:
                        scheduler._DUE[scheduler._job_key(j["id"])] = late
                    scheduler._fire_job_threadsafe(j["id"])
                with scheduler._DUE_LOCK:
                    scheduler._DUE[scheduler._job_key(jobs[3]["id"])] = \
                        datetime.now(timezone.utc)
                scheduler._fire_job_threadsafe(jobs[3]["id"])

                async def settle():
                    for _ in range(10):
                        await asyncio.sleep(0)
                    await pacer.drain()

                loop.run_until_complete(settle())
        finally:
            loop.close()
        ids = [j for j, _ in fired]
        self.assertEqual(sorted(ids), sorted(j["id"] for j in jobs))
        late_fires = [t for j, t in fired if j != jobs[3]["id"]]
        gaps = [(b - a).total_seconds() for a, b in zip(late_fires, late_fires[1:])]
        self.assertEqual(gaps, [90.0, 90.0])
        self.assertEqual(len(pacer.released), 3, "the punctual fire bypassed the queue")


class DueStampTests(unittest.TestCase):
    def test_the_executor_stamps_the_due_time(self):
        class _Job:
            id = scheduler._job_key(4242)

        due = datetime(2026, 11, 21, 7, 20, tzinfo=timezone.utc)
        with patch("apscheduler.executors.pool.BasePoolExecutor._do_submit_job",
                   lambda self, job, run_times: None):
            scheduler._DueStampingExecutor()._do_submit_job(_Job(), [due])
        self.assertEqual(scheduler._take_due(4242), due)
        self.assertIsNone(scheduler._take_due(4242), "read once")


if __name__ == "__main__":
    unittest.main()
