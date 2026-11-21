"""Staggered catch-up of missed fires after a host wake (clodia-platform#711).

21 Nov 2026: the host slept ~1.6 h; at wake APScheduler fired every missed job
in the same second (the daily GRC digest, a topic trigger), on top of the
courier and the topic indexer. The gateway sat at 100% CPU with 30–45 s
event-loop stalls for ~8 minutes. Same on 16, 17 and 18 Nov.

`coalesce=True` already collapses each job's backlog into ONE fire, and the
cadence-derived grace (#273) already decides WHETHER a late fire still runs.
What was missing is WHEN: every job's single catch-up fired at once. This
module is the queue in between:

- a catch-up fire is not run when APScheduler submits it; it is `offer`ed to
  the pacer, which releases one entry at a time, spaced by `spacing_s` plus a
  random `jitter_s`, with at most `max_concurrent` catch-ups in flight;
- programmatic (logic) jobs go first, agentic ones after; within a class the
  earliest grace deadline goes first;
- at most one catch-up per job: a second fire of a job already queued or in
  flight is collapsed into it;
- the grace is honoured: an entry whose deadline passed while it waited is not
  run;
- nothing disappears in silence: every collapse and every expiry goes through
  `on_drop`, which the scheduler wires to a log line AND a `missed` run row.

The pacer is pure asyncio with an injectable clock and sleep, so a clock jump
with several missed jobs can be simulated in a test without waiting.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

LOG = logging.getLogger("scheduler.catch_up")


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    """A non-negative number from the environment; the default if unreadable.
    A typo in a compose file must not take the scheduler down."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        LOG.warning("%s=%r is not a number: using %s", name, raw, default)
        return default
    return max(minimum, value)


@dataclass(frozen=True)
class CatchUpSettings:
    """Knobs of the catch-up, all overridable from the environment.

    Defaults follow the issue: one catch-up at a time, 60–120 s apart.
    """
    #: Minimum gap between two releases.
    spacing_s: float = 60.0
    #: Random extra gap in [0, jitter_s] added to each spacing.
    jitter_s: float = 60.0
    #: Catch-ups running at the same time.
    max_concurrent: int = 1
    #: How long an agentic catch-up keeps its slot while its turn runs. Past
    #: this the next entry is released anyway: a hung turn must not freeze
    #: every other recovery.
    hold_s: float = 1800.0
    #: A wall-clock jump larger than this (wall elapsed minus monotonic
    #: elapsed) is a host wake: enter catch-up mode.
    wall_jump_s: float = 120.0
    #: How long catch-up mode lasts after a detected wake: during it even a
    #: punctual fire goes through the queue, because it lands in the same burst.
    window_s: float = 600.0

    @classmethod
    def from_env(cls) -> "CatchUpSettings":
        d = cls()
        return cls(
            spacing_s=_env_float("CLODIA_CATCHUP_SPACING_S", d.spacing_s),
            jitter_s=_env_float("CLODIA_CATCHUP_JITTER_S", d.jitter_s),
            max_concurrent=int(_env_float("CLODIA_CATCHUP_MAX_CONCURRENT",
                                          d.max_concurrent, minimum=1)),
            hold_s=_env_float("CLODIA_CATCHUP_HOLD_S", d.hold_s),
            wall_jump_s=_env_float("CLODIA_CATCHUP_WALL_JUMP_S", d.wall_jump_s,
                                   minimum=1.0),
            window_s=_env_float("CLODIA_CATCHUP_WINDOW_S", d.window_s),
        )


@dataclass
class CatchUpEntry:
    """One fire waiting to be recovered."""
    job_id: int
    #: The fire being recovered (the latest one, after coalescing).
    scheduled: datetime
    #: `scheduled` + the job's grace: past this the fire is not run.
    deadline: datetime
    #: Logic jobs (no LLM turn) are released before agentic ones.
    programmatic: bool = False
    #: Why it is a catch-up, for the log ("late 94 min", "boot", "wake").
    reason: str = ""
    #: Fires collapsed into this entry (itself included).
    fires: int = 1
    queued_at: Optional[datetime] = field(default=None, compare=False)

    def sort_key(self) -> tuple:
        return (0 if self.programmatic else 1, self.deadline, self.scheduled,
                self.job_id)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


Runner = Callable[[int], Awaitable[object]]
DropHook = Callable[[CatchUpEntry, str], None]


class CatchUpPacer:
    """The queue of catch-up fires. Lives on ONE asyncio loop: `offer` must be
    called from that loop's thread (the scheduler hops there with
    `call_soon_threadsafe`)."""

    def __init__(self, runner: Runner, *,
                 settings: Optional[CatchUpSettings] = None,
                 on_drop: Optional[DropHook] = None,
                 now: Callable[[], datetime] = _utcnow,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 rand: Callable[[], float] = random.random) -> None:
        self._runner = runner
        self.settings = settings or CatchUpSettings()
        self._on_drop = on_drop
        self._now = now
        self._sleep = sleep
        self._rand = rand
        self._queue: dict[int, CatchUpEntry] = {}
        self._inflight: dict[int, CatchUpEntry] = {}
        self._tasks: set[asyncio.Task] = set()
        self._worker: Optional[asyncio.Task] = None
        self._slot_freed = asyncio.Event()
        self._next_release_at: Optional[datetime] = None
        #: Release log, (job_id, when): the observable the tests assert on.
        self.released: list[tuple[int, datetime]] = []

    # -- state ---------------------------------------------------------------

    def pending(self) -> list[int]:
        return [e.job_id for e in sorted(self._queue.values(),
                                         key=CatchUpEntry.sort_key)]

    def inflight(self) -> list[int]:
        return list(self._inflight)

    def busy(self) -> bool:
        return bool(self._queue or self._inflight)

    # -- intake --------------------------------------------------------------

    def offer(self, entry: CatchUpEntry) -> str:
        """Queue a catch-up. Returns `queued` or `coalesced`.

        A job already queued keeps ONE entry, moved to the most recent fire
        (and its later deadline): the older fire is the one collapsed, which is
        what `coalesce=True` would have done had both been due at once. A job
        already in flight is not queued again: the run in progress covers it.
        Either way the collapsed fire is reported, never just forgotten.
        """
        entry.queued_at = entry.queued_at or self._now()
        running = self._inflight.get(entry.job_id)
        if running is not None:
            self._drop(entry, "collapsed: a catch-up of this job is already "
                              f"running (fire {running.scheduled.isoformat()})")
            return "coalesced"
        queued = self._queue.get(entry.job_id)
        if queued is not None:
            older, newer = sorted((queued, entry), key=lambda e: e.scheduled)
            newer.fires = queued.fires + entry.fires
            newer.queued_at = queued.queued_at
            newer.programmatic = queued.programmatic or entry.programmatic
            self._queue[entry.job_id] = newer
            self._drop(older, "collapsed into the pending catch-up of fire "
                              f"{newer.scheduled.isoformat()}")
            return "coalesced"
        self._queue[entry.job_id] = entry
        LOG.warning("Catch-up QUEUED job id=%s: fire %s (%s); %d queued, %d "
                    "running", entry.job_id, entry.scheduled.isoformat(),
                    entry.reason or "late", len(self._queue), len(self._inflight))
        self._ensure_worker()
        return "queued"

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.ensure_future(self._work())
        self._slot_freed.set()

    # -- release -------------------------------------------------------------

    async def _work(self) -> None:
        while self._queue:
            if len(self._inflight) >= self.settings.max_concurrent:
                self._slot_freed.clear()
                await self._slot_freed.wait()
                continue
            now = self._now()
            if self._next_release_at is not None and now < self._next_release_at:
                await self._sleep((self._next_release_at - now).total_seconds())
                continue
            entry = min(self._queue.values(), key=CatchUpEntry.sort_key)
            del self._queue[entry.job_id]
            if now > entry.deadline:
                late = (now - entry.scheduled).total_seconds() / 60.0
                self._drop(entry, f"past its grace while waiting in the "
                                  f"catch-up queue ({late:.0f} min late)")
                continue
            self._release(entry, now)

    def _release(self, entry: CatchUpEntry, now: datetime) -> None:
        gap = self.settings.spacing_s + self.settings.jitter_s * self._rand()
        self._next_release_at = now + timedelta(seconds=gap)
        self._inflight[entry.job_id] = entry
        self.released.append((entry.job_id, now))
        LOG.warning("Catch-up RELEASED job id=%s: fire %s, %.0f min late%s; "
                    "next release in >= %.0fs, %d still queued",
                    entry.job_id, entry.scheduled.isoformat(),
                    (now - entry.scheduled).total_seconds() / 60.0,
                    f" ({entry.fires} fires collapsed)" if entry.fires > 1 else "",
                    gap, len(self._queue))
        task = asyncio.ensure_future(self._run(entry))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, entry: CatchUpEntry) -> None:
        try:
            await self._runner(entry.job_id)
        except Exception as e:  # noqa: BLE001 — the run records its own error
            LOG.error("Catch-up of job id=%s failed: %s: %s",
                      entry.job_id, type(e).__name__, e)
        finally:
            self._inflight.pop(entry.job_id, None)
            self._slot_freed.set()

    def _drop(self, entry: CatchUpEntry, why: str) -> None:
        LOG.warning("Catch-up DROPPED job id=%s fire %s: %s",
                    entry.job_id, entry.scheduled.isoformat(), why)
        if self._on_drop is None:
            return
        try:
            self._on_drop(entry, why)
        except Exception as e:  # noqa: BLE001 — recording must not stop the queue
            LOG.error("Catch-up of job id=%s: drop not recorded (%s)",
                      entry.job_id, e)

    async def drain(self) -> None:
        """Wait until nothing is queued or running (tests, shutdown)."""
        while True:
            if self._worker is not None and not self._worker.done():
                await self._worker
                continue
            running = [t for t in self._tasks if not t.done()]
            if not running:
                return
            await asyncio.wait(running)


class WakeDetector:
    """Detects a host wake from the two clocks (clodia-platform#711).

    While the host sleeps the MONOTONIC clock stops and the WALL clock does not
    (#530): the difference between how much each moved since the previous tick
    is the time spent asleep. Past `wall_jump_s` the scheduler enters catch-up
    mode for `window_s`.
    """

    def __init__(self, settings: CatchUpSettings, *,
                 wall: Callable[[], float], mono: Callable[[], float]) -> None:
        self.settings = settings
        self._wall = wall
        self._mono = mono
        self._last: Optional[tuple[float, float]] = None
        self._mode_until_mono: Optional[float] = None

    def tick(self) -> Optional[float]:
        """Sample the clocks; the seconds slept if a wake is detected."""
        w, m = self._wall(), self._mono()
        last, self._last = self._last, (w, m)
        if last is None:
            return None
        slept = (w - last[0]) - (m - last[1])
        if slept > self.settings.wall_jump_s:
            self.enter(m)
            return slept
        return None

    def enter(self, mono_now: Optional[float] = None) -> None:
        start = self._mono() if mono_now is None else mono_now
        self._mode_until_mono = start + self.settings.window_s

    def active(self) -> bool:
        return (self._mode_until_mono is not None
                and self._mono() < self._mode_until_mono)
