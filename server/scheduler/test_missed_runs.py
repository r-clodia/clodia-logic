"""I fire che non avvengono (clodia-platform#273).

Il difetto misurato il 23 ago 2026: il portatile che ospita l'istanza `personal`
ha dormito dieci ore, APScheduler ha scartato i fire come misfire — il grace era
60s fissi per ogni job — la callback non è mai partita e quindi **nessuno ha
registrato niente**. Due notti di backup ISO 27001 A.8.13 mancanti, `last_status`
a `ok`.

Qui si verificano le tre cose che devono valere perché quel silenzio non torni:

  1. il grace è la CADENZA del job, quindi un giornaliero sopravvive a dieci ore
     di sonno (e un trigger di topic no, di proposito);
  2. un fire scartato lascia un run in stato `missed`, e `last_status` smette di
     dire che l'ultima volta è andata bene;
  3. un job fermo da più della sua cadenza è STALE anche se nessuno sa perché —
     è l'unico controllo che non dipende dal meccanismo del misfire.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, scheduler


class _FakeMissedEvent:
    """`EVENT_JOB_MISSED` porta l'id APScheduler e l'orario previsto del fire."""

    def __init__(self, job_id: str, scheduled_run_time: datetime) -> None:
        self.job_id = job_id
        self.scheduled_run_time = scheduled_run_time


class _CapturingScheduler:
    """Cattura i kwargs di `add_job` senza avviare nulla."""

    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def add_job(self, *args, **kwargs) -> None:
        self.jobs.append(kwargs)


class JobsDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = db.JOBS_DIR
        db.JOBS_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        db.JOBS_DIR = self._old_dir
        self._tmp.cleanup()


class MisfireGraceTests(unittest.TestCase):
    """Il grace deve stare alla scala della cadenza, non a 60 secondi."""

    def test_daily_job_survives_a_ten_hour_sleep(self) -> None:
        # Il caso reale: backup notturno `0 0 * * *`, misfire di 10h56m.
        grace = scheduler.misfire_grace_for(
            {"cron_expr": "0 0 * * *", "mode": "agentic"})
        self.assertGreaterEqual(
            grace, int(timedelta(hours=10, minutes=56).total_seconds()),
            "un backup giornaliero perso mentre la macchina dorme dieci ore "
            "deve essere ancora recuperabile al risveglio")

    def test_hourly_job_recovers_within_its_own_period(self) -> None:
        grace = scheduler.misfire_grace_for(
            {"cron_expr": "0 * * * *", "mode": "agentic"})
        self.assertEqual(grace, 3600)

    def test_interval_job_uses_its_interval(self) -> None:
        grace = scheduler.misfire_grace_for(
            {"interval_minutes": 30, "mode": "agentic"})
        self.assertEqual(grace, 1800)

    def test_topic_trigger_is_not_recovered(self) -> None:
        # Un prompt di sorveglianza consegnato dieci ore dopo non sorveglia
        # niente: qui il grace corto è la decisione, non una dimenticanza.
        grace = scheduler.misfire_grace_for(
            {"interval_minutes": 60, "mode": "topic_trigger"})
        self.assertEqual(grace, 60)

    def test_weekly_job_is_capped_at_one_day(self) -> None:
        grace = scheduler.misfire_grace_for(
            {"cron_expr": "30 0 * * 0", "mode": "agentic"})
        self.assertEqual(grace, 24 * 3600)

    def test_unreadable_cadence_keeps_the_historic_floor(self) -> None:
        grace = scheduler.misfire_grace_for({"cron_expr": "", "mode": "agentic"})
        self.assertEqual(grace, 60)


class RegisterJobWiringTests(JobsDirTestCase):
    """Il grace calcolato deve arrivare davvero ad APScheduler."""

    def test_register_job_passes_the_derived_grace(self) -> None:
        job = db.create_job("backup", "0 0 * * *", "backup della piattaforma")
        finto = _CapturingScheduler()
        vecchio = scheduler._scheduler
        scheduler._scheduler = finto
        try:
            scheduler.register_job(job)
        finally:
            scheduler._scheduler = vecchio
        self.assertEqual(len(finto.jobs), 1)
        self.assertEqual(finto.jobs[0]["misfire_grace_time"], 24 * 3600)
        # `coalesce` era già così e deve restarlo: al risveglio i fire arretrati
        # collassano in uno, altrimenti il recupero diventerebbe una raffica.
        self.assertTrue(finto.jobs[0]["coalesce"])


class MissedRunIsRecordedTests(JobsDirTestCase):
    """Un fire scartato deve lasciare un run, non il silenzio."""

    def test_missed_fire_creates_a_run(self) -> None:
        job = db.create_job("backup", "0 0 * * *", "backup della piattaforma")
        db.mark_run(job["id"], status="ok", chat_id=None)  # l'ultima volta andò bene
        previsto = datetime.now(timezone.utc) - timedelta(hours=10, minutes=56)
        scheduler._on_job_missed(
            _FakeMissedEvent(f"clodia-job-{job['id']}", previsto))
        stored = db.get_job(job["id"])
        self.assertEqual(stored["runs"][-1]["stato"], "missed")
        self.assertNotEqual(
            stored["runs"][-1]["stato"], "success",
            "un fire scartato non è un run andato bene")
        # Il registro non deve più dire `ok` quando non è partito niente.
        self.assertTrue(stored["last_status"].startswith("missed"),
                        f"last_status = {stored['last_status']!r}")
        # Il motivo va letto da chi apre lo storico, non solo dai log.
        self.assertIn("ritardo", stored["runs"][-1]["note"])

    def test_missed_run_is_not_a_terminal_state(self) -> None:
        # `complete_run` accetta gli esiti di un run PARTITO: `missed` non è uno
        # di quelli, e ammetterlo lì significherebbe che un run mai iniziato può
        # avere una durata.
        self.assertNotIn(db.MISSED, db.TERMINAL_STATES)

    def test_event_of_another_scheduler_is_ignored(self) -> None:
        db.create_job("backup", "0 0 * * *", "x")
        scheduler._on_job_missed(
            _FakeMissedEvent("qualcun-altro-42", datetime.now(timezone.utc)))
        self.assertEqual(db.get_job(1)["runs"] or [], [])


class StalenessTests(JobsDirTestCase):
    """Il controllo che guarda il risultato invece del meccanismo."""

    def test_job_stuck_for_two_nights_is_stale(self) -> None:
        job = db.create_job("backup", "0 0 * * *", "x")
        due_notti_fa = datetime.now(timezone.utc) - timedelta(days=2, hours=9)
        db.mark_run(job["id"], status="ok", chat_id=None)
        aggiornato = db.get_job(job["id"])
        aggiornato["last_run_at"] = due_notti_fa.isoformat()
        motivo = scheduler.stale_reason(aggiornato)
        self.assertIsNotNone(motivo, "un backup giornaliero fermo da due notti "
                                    "non può risultare fresco")
        self.assertIn("senza run", motivo)

    def test_job_that_just_ran_is_fresh(self) -> None:
        job = db.create_job("backup", "0 0 * * *", "x")
        db.mark_run(job["id"], status="ok", chat_id=None)
        self.assertIsNone(scheduler.stale_reason(db.get_job(job["id"])))

    def test_job_that_never_ran_is_measured_from_creation(self) -> None:
        job = db.create_job("backup", "0 0 * * *", "x")
        job["created_at"] = (
            datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        self.assertIsNotNone(scheduler.stale_reason(job))

    def test_disabled_job_is_never_stale(self) -> None:
        job = db.create_job("backup", "0 0 * * *", "x", enabled=False)
        job["last_run_at"] = (
            datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        self.assertIsNone(scheduler.stale_reason(job),
                          "un job disabilitato non doveva partire")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
