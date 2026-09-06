"""I fire mai GENERATI mentre il processo era giù (clodia-platform#289).

#273 e #287 coprono il fire che APScheduler ha visto e scartato, o eseguito in
ritardo: in entrambi i casi esiste un evento, e da lì un listener può scrivere
una riga. Il caso che ha prodotto davvero le tre notti del 22/23/24 ago 2026 è
un altro e non ha nessun evento: **il processo era giù**. Il jobstore è
in-memory, al riavvio `add_job` ricalcola il prossimo fire da adesso, e i fire
di quelle notti non sono mai esistiti per nessuno — nello storico di `id=2` non
c'è alcuna riga fra il 21 ago e oggi.

L'unico momento in cui quel buco è ancora deducibile è il **boot**: si conosce
`last_run_at`, si conosce il trigger, e la differenza fra i due è la lista dei
fire che sarebbero dovuti avvenire.

I tre punti che la issue chiedeva di decidere prima di scrivere codice, e la
risposta che questi test sorvegliano:

(a) **cap**: un job ogni 10 minuti fermo da un mese sono 4.320 righe, e
    `_RUNS_CAP` le mangerebbe tutte cancellando lo storico buono. Si scrivono le
    più recenti fino a `_BACKFILL_MAX_ROWS`, più UNA riga che dice quante ne
    mancano — il conto non si perde, lo storico nemmeno;
(b) **idempotenza**: un filigrana (`missed_scan_at`) segna fin dove si è già
    guardato, così due riavvii ravvicinati non riscrivono lo stesso fire;
(c) **`last_status` non si tocca**: al boot non è successo niente, e riscrivere
    l'esito dell'ultimo run cambierebbe ciò che l'utente vede senza che nessuno
    abbia eseguito nulla. Il titolo lo dice già lo stesso: `stale` (#287) è
    calcolato in lettura e non dipende da questa riconciliazione.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, scheduler


#: Un'ora FISSA per «adesso». I conti su un cron dipendono dall'ora del giorno —
#: tre notti o quattro, secondo che il fermo cominci prima o dopo mezzanotte — e
#: un test che cambia risposta secondo quando lo si esegue non sorveglia niente.
ORA = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


class JobsDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = db.JOBS_DIR
        db.JOBS_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        db.JOBS_DIR = self._old_dir
        self._tmp.cleanup()

    def _job_fermo_da(self, quanto: timedelta, *, fino_a: datetime = ORA,
                      **kw) -> dict:
        """Un job il cui ultimo run risale a `quanto` fa: il processo era giù."""
        job = db.create_job(kw.pop("name", "backup"),
                            kw.pop("cron_expr", "0 0 * * *"),
                            kw.pop("prompt", "backup della piattaforma"), **kw)
        db.mark_run(job["id"], status="ok", chat_id=None)  # l'ultima volta andò bene
        d = db.get_job(job["id"])
        d["last_run_at"] = (fino_a - quanto).isoformat()
        db._write(d)
        return db.get_job(job["id"])


class TheHoleIsFoundTests(JobsDirTestCase):
    def test_three_missing_nights_leave_three_rows(self) -> None:
        """Il caso della issue: backup giornaliero, processo giù tre notti."""
        job = self._job_fermo_da(timedelta(days=3, hours=1))
        scritte = scheduler.backfill_missed_fires(job, now=ORA)
        self.assertEqual(scritte, 3)
        runs = db.get_job(job["id"])["runs"]
        persi = [r for r in runs if r["stato"] == db.MISSED]
        self.assertEqual(len(persi), 3)

    def test_the_row_is_dated_the_fire_not_the_boot(self) -> None:
        """Una riga datata al riavvio direbbe «è successo adesso» di una cosa
        successa stanotte, e lo storico servirebbe a niente: la data del fire
        mancante È l'informazione."""
        job = self._job_fermo_da(timedelta(days=3, hours=1))
        scheduler.backfill_missed_fires(job, now=ORA)
        persi = [r for r in db.get_job(job["id"])["runs"]
                 if r["stato"] == db.MISSED]
        for r in persi:
            self.assertLess(datetime.fromisoformat(r["ts"]),
                            ORA - timedelta(hours=1))
        # e sono in ordine, perché lo storico si legge dall'alto
        self.assertEqual([r["ts"] for r in persi],
                         sorted(r["ts"] for r in persi))

    def test_the_note_says_it_was_never_generated(self) -> None:
        """Un `missed` scartato da APScheduler e un fire mai generato hanno
        rimedi diversi (il grace, contro il processo che era giù): la riga deve
        dire quale dei due è."""
        job = self._job_fermo_da(timedelta(days=2, hours=1))
        scheduler.backfill_missed_fires(job, now=ORA)
        nota = [r for r in db.get_job(job["id"])["runs"]
                if r["stato"] == db.MISSED][-1]["note"]
        self.assertIn("mai generato", nota)

    def test_a_job_that_did_not_miss_anything_writes_nothing(self) -> None:
        """Il rumore ucciderebbe il segnale, e una scrittura per job a ogni boot
        cambierebbe `updated_at` senza che sia successo niente."""
        job = self._job_fermo_da(timedelta(hours=2))  # cadenza 24h
        prima = db.get_job(job["id"])["updated_at"]
        self.assertEqual(scheduler.backfill_missed_fires(job, now=ORA), 0)
        dopo = db.get_job(job["id"])
        self.assertEqual([r for r in dopo["runs"] if r["stato"] == db.MISSED], [])
        self.assertEqual(dopo["updated_at"], prima)

    def test_an_interval_job_is_walked_too(self) -> None:
        """`interval_minutes` è l'altra forma di periodicità (#239): il trigger
        dell'intervallo non ha una start_date nel passato, e senza dargliela il
        cammino non troverebbe nulla — cioè il difetto resterebbe aperto proprio
        per i trigger di topic."""
        job = self._job_fermo_da(timedelta(days=1), name="trigger",
                                 mode="topic_trigger")
        d = db.get_job(job["id"])
        d["interval_minutes"] = 360  # ogni 6 ore → 4 fire in un giorno
        db._write(d)
        scritte = scheduler.backfill_missed_fires(db.get_job(job["id"]), now=ORA)
        self.assertEqual(scritte, 4)

    def test_a_job_that_never_ran_is_measured_from_creation(self) -> None:
        job = db.create_job("backup", "0 0 * * *", "x")
        d = db.get_job(job["id"])
        d["created_at"] = (ORA - timedelta(days=2, hours=1)).isoformat()
        db._write(d)
        self.assertEqual(
            scheduler.backfill_missed_fires(db.get_job(job["id"]), now=ORA), 2)

    def test_a_disabled_job_missed_nothing(self) -> None:
        job = self._job_fermo_da(timedelta(days=10), enabled=False)
        self.assertEqual(scheduler.backfill_missed_fires(job, now=ORA), 0)


class TheCapProtectsTheGoodHistoryTests(JobsDirTestCase):
    """(a) Il cap, e la ragione per cui non è solo un troncamento."""

    def test_a_ten_minute_job_down_for_a_month_does_not_erase_the_history(self) -> None:
        job = self._job_fermo_da(timedelta(days=30), name="trigger",
                                 cron_expr="*/10 * * * *")
        for _ in range(39):  # 40 righe buone in tutto, da preservare
            db.mark_run(job["id"], status="ok", chat_id=None)
        d = db.get_job(job["id"])
        d["last_run_at"] = (ORA - timedelta(days=30)).isoformat()
        db._write(d)
        scheduler.backfill_missed_fires(db.get_job(job["id"]), now=ORA)
        runs = db.get_job(job["id"])["runs"]
        buoni = [r for r in runs if r["stato"] == "success"]
        self.assertEqual(len(buoni), 40,
                         "le 4.320 righe retroattive avrebbero mangiato tutto "
                         "lo storico buono passando dal cap di _RUNS_CAP")
        self.assertLessEqual(len([r for r in runs if r["stato"] == db.MISSED]),
                             scheduler._BACKFILL_MAX_ROWS + 1)

    def test_the_count_survives_the_cap(self) -> None:
        """Troncare e basta perderebbe il numero, che è l'unica cosa che dice
        quanto è grave: una riga di sintesi lo tiene."""
        job = self._job_fermo_da(timedelta(days=30), cron_expr="*/10 * * * *")
        scheduler.backfill_missed_fires(job, now=ORA)
        persi = [r for r in db.get_job(job["id"])["runs"]
                 if r["stato"] == db.MISSED]
        sintesi = persi[0]["note"]
        self.assertIn("altri", sintesi)
        self.assertIn("non elencati", sintesi)
        # il numero deve esserci: 4.320 fire, 20 elencati
        self.assertIn(str(4320 - scheduler._BACKFILL_MAX_ROWS), sintesi)

    def test_the_walk_itself_is_bounded(self) -> None:
        """Il cap sulle RIGHE non basta: camminare un trigger al minuto per un
        mese sono 43.200 iterazioni al boot, e il conto esatto vale meno di un
        avvio che parte. Oltre il tetto si dice «almeno»."""
        job = self._job_fermo_da(timedelta(days=30), cron_expr="* * * * *")
        fires, troncato = scheduler.missed_fires_since(
            db.get_job(job["id"]), ORA - timedelta(days=30), ORA)
        self.assertTrue(troncato)
        self.assertEqual(len(fires), scheduler._BACKFILL_MAX_ITER)
        scheduler.backfill_missed_fires(db.get_job(job["id"]), now=ORA)
        persi = [r for r in db.get_job(job["id"])["runs"]
                 if r["stato"] == db.MISSED]
        # Con il cammino troncato i fire in mano sono i più VECCHI del buco:
        # elencarne venti darebbe una finestra presa dal mezzo spacciandola per
        # la coda. Si scrive solo il fatto, che è quello certo.
        self.assertEqual(len(persi), 1)
        self.assertIn("almeno", persi[0]["note"])

    def test_the_horizon_bounds_a_job_stopped_forever(self) -> None:
        """Un job fermo da sei mesi non ha bisogno di seimila righe per dire che
        è fermo: lo dice `stale` in una riga sola."""
        job = self._job_fermo_da(timedelta(days=180))
        scheduler.backfill_missed_fires(job, now=ORA)
        persi = [r for r in db.get_job(job["id"])["runs"]
                 if r["stato"] == db.MISSED]
        piu_vecchio = datetime.fromisoformat(persi[0]["ts"])
        self.assertGreaterEqual(
            piu_vecchio,
            ORA - timedelta(days=scheduler._BACKFILL_HORIZON_DAYS + 1))


class IdempotenceTests(JobsDirTestCase):
    """(b) Due riavvii ravvicinati non scrivono due volte lo stesso fire."""

    def test_a_second_boot_writes_nothing_new(self) -> None:
        job = self._job_fermo_da(timedelta(days=3, hours=1))
        primo = scheduler.backfill_missed_fires(db.get_job(job["id"]), now=ORA)
        secondo = scheduler.backfill_missed_fires(db.get_job(job["id"]), now=ORA)
        self.assertEqual((primo, secondo), (3, 0))
        persi = [r for r in db.get_job(job["id"])["runs"]
                 if r["stato"] == db.MISSED]
        self.assertEqual(len(persi), 3)

    def test_the_watermark_survives_a_write(self) -> None:
        """La filigrana sta nel record del job: se `_write` non la portasse
        (scrive solo i campi dichiarati), sparirebbe al primo aggiornamento e
        l'idempotenza durerebbe fino al fire successivo."""
        job = self._job_fermo_da(timedelta(days=3, hours=1))
        scheduler.backfill_missed_fires(job, now=ORA)
        db.mark_run(job["id"], status="ok", chat_id=None)
        self.assertTrue(db.get_job(job["id"])["missed_scan_at"])

    def test_a_fire_after_the_watermark_is_still_found(self) -> None:
        """L'idempotenza non deve diventare cecità: un secondo periodo di
        fermo, dopo una riconciliazione, va visto."""
        job = self._job_fermo_da(timedelta(days=3, hours=1))
        scheduler.backfill_missed_fires(db.get_job(job["id"]), now=ORA)
        # il processo riparte due giorni dopo, senza che nulla sia girato
        self.assertEqual(
            scheduler.backfill_missed_fires(db.get_job(job["id"]),
                                            now=ORA + timedelta(days=2)), 2)


class WhatMustNotChangeTests(JobsDirTestCase):
    """(c) E il resto di ciò che l'utente vede."""

    def test_the_last_run_fields_are_not_rewritten(self) -> None:
        job = self._job_fermo_da(timedelta(days=3, hours=1))
        prima = db.get_job(job["id"])
        scheduler.backfill_missed_fires(job, now=ORA)
        dopo = db.get_job(job["id"])
        self.assertEqual(dopo["last_status"], prima["last_status"])
        self.assertEqual(dopo["last_run_at"], prima["last_run_at"])

    def test_the_job_stays_stale(self) -> None:
        """Se la riconciliazione toccasse `last_run_at`, il job risulterebbe
        appena girato e il badge STALE si spegnerebbe: la traccia avrebbe
        cancellato l'allarme che la rendeva utile."""
        adesso = datetime.now(timezone.utc)
        job = self._job_fermo_da(timedelta(days=3, hours=1), fino_a=adesso)
        scheduler.backfill_missed_fires(job, now=adesso)
        self.assertIsNotNone(scheduler.stale_reason(db.get_job(job["id"])))

    def test_a_backfilled_row_is_not_a_terminal_state(self) -> None:
        job = self._job_fermo_da(timedelta(days=3, hours=1))
        scheduler.backfill_missed_fires(job, now=ORA)
        for r in db.get_job(job["id"])["runs"]:
            if r["stato"] == db.MISSED:
                self.assertNotIn(r["stato"], db.TERMINAL_STATES)

    def test_an_unreadable_cadence_is_skipped_not_guessed(self) -> None:
        job = self._job_fermo_da(timedelta(days=3, hours=1))
        d = db.get_job(job["id"])
        d["cron_expr"] = "non un cron"
        db._write(d)
        self.assertEqual(
            scheduler.backfill_missed_fires(db.get_job(job["id"]), now=ORA), 0)

    def test_the_boot_reconciles_every_enabled_job(self) -> None:
        """Il cablaggio: la funzione esiste, ma se il boot non la chiama la
        issue resta aperta."""
        adesso = datetime.now(timezone.utc)
        self._job_fermo_da(timedelta(days=3, hours=1), fino_a=adesso,
                           name="backup")
        self._job_fermo_da(timedelta(days=3, hours=1), fino_a=adesso,
                           name="digest")
        visti: list[int] = []
        vecchio_sched, vecchio_reg = scheduler._scheduler, scheduler.register_job
        scheduler._scheduler = type("S", (), {"remove_all_jobs": lambda self: None})()
        scheduler.register_job = lambda job: visti.append(job["id"])
        try:
            scheduler.reload_all_enabled_jobs()
        finally:
            scheduler._scheduler, scheduler.register_job = vecchio_sched, vecchio_reg
        self.assertEqual(sorted(visti), [1, 2])
        for job_id in (1, 2):
            self.assertTrue([r for r in db.get_job(job_id)["runs"]
                             if r["stato"] == db.MISSED],
                            f"job {job_id}: il boot non ha riconciliato")

    def test_a_failing_reconciliation_does_not_stop_the_boot(self) -> None:
        """Un boot che non registra i job perché la riconciliazione è esplosa
        sarebbe un rimedio peggiore del difetto: lì i fire smetterebbero di
        avvenire davvero."""
        self._job_fermo_da(timedelta(days=3, hours=1),
                           fino_a=datetime.now(timezone.utc), name="backup")
        visti: list[int] = []
        vecchio_sched = scheduler._scheduler
        vecchio_reg, vecchio_bf = scheduler.register_job, scheduler.backfill_missed_fires
        scheduler._scheduler = type("S", (), {"remove_all_jobs": lambda self: None})()
        scheduler.register_job = lambda job: visti.append(job["id"])

        def _boom(*a, **k):
            raise RuntimeError("trigger illeggibile")

        scheduler.backfill_missed_fires = _boom
        try:
            n = scheduler.reload_all_enabled_jobs()
        finally:
            scheduler._scheduler = vecchio_sched
            scheduler.register_job, scheduler.backfill_missed_fires = (
                vecchio_reg, vecchio_bf)
        self.assertEqual((n, visti), (1, [1]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
