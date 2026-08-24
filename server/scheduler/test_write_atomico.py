"""Due scritture concorrenti non corrompono il file di un job (#291).

Il 24 ago 2026, sull'istanza, `jobs/6.yaml` è finito con un frammento orfano
(`:00'`) e **due** `updated_at` a 15 millisecondi di distanza. Da lì il job —
un trigger di topic attivo — è diventato invisibile: `_read` scarta un file che
non parsa, `_all` salta il `None`, e nessun log lo diceva.

I due scrittori erano i due listener dello scheduler per lo **stesso** fire,
`_on_job_missed` e `_on_job_submitted`, che nei log compaiono nello stesso
millisecondo:

    19:25:53,862 WARNING Job RECUPERATO id=6
    19:25:53,862 WARNING Job MISSED id=6

La causa era `_write` che faceva `Path.write_text`: apre in troncamento e
riscrive, quindi due scritture sovrapposte lasciano in coda i byte della più
lunga.

La dinamica: due `open(mode='w')` sullo stesso percorso hanno offset
**indipendenti**. Se entrambi troncano e poi il più lungo scrive dopo il più
corto, il risultato ha i primi byte del corto e la **coda** del lungo — che è
esattamente il `:00'` seguito da un secondo `updated_at`.

Questi test provano la proprietà (**il file parsa sempre**), non il rimedio: non
guardano se c'è un lock o un `os.replace`, così restano validi se la
sincronizzazione cambia forma.

Onestà su cosa provano: il test a thread concorrenti **non riproduce** il guasto
sul codice vecchio in modo affidabile — la finestra è di microsecondi e su
`main` passa. Serve come guardia contro una regressione, non come prova della
causa. La prova della dinamica è `test_la_dinamica_del_guasto_...`, che la
ricostruisce in modo deterministico su una replica del codice sostituito.
"""
from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import yaml


class ScrittureConcorrentiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "jobs"
        # `JOBS_DIR` è risolta all'import da `data_path`: si sostituisce il
        # modulo, non l'ambiente, per non dipendere da CLODIA_DATA.
        from server.scheduler import db

        self.db = db
        self._patch = mock.patch.object(db, "JOBS_DIR", self.dir)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def _job(self, note: str) -> dict:
        return {
            "id": 6,
            "name": "topic-trigger:SEAL-1/software-house",
            "cron_expr": "",
            "prompt": "x",
            "agent": "clodia",
            "enabled": True,
            "mode": "topic_trigger",
            "runs": [{"id": "1", "stato": "running", "note": note}],
            "last_status": note,
            "updated_at": "2026-08-24T19:25:53.856266+00:00",
        }

    def test_due_scritture_simultanee_lasciano_un_file_leggibile(self):
        """Il cuore. Due scrittori con payload di lunghezza molto diversa,
        ripetuti: dopo ogni giro il file deve parsare. Sul `write_text` diretto
        questo test trova il miscuglio."""
        lungo = self._job("fire previsto 2026-08-24T21:14:52.815239+02:00 non "
                          "eseguito (ritardo 11 min, oltre il grace del job) "
                          + "x" * 400)
        corto = self._job("ok")
        errori: list[str] = []

        def scrivi(d, n):
            for _ in range(n):
                try:
                    self.db._write(d)
                except Exception as e:  # noqa: BLE001
                    errori.append(f"scrittura: {e!r}")

        for giro in range(12):
            t1 = threading.Thread(target=scrivi, args=(lungo, 6))
            t2 = threading.Thread(target=scrivi, args=(corto, 6))
            t1.start(); t2.start(); t1.join(); t2.join()
            testo = (self.dir / "6.yaml").read_text(encoding="utf-8")
            try:
                d = yaml.safe_load(testo)
            except yaml.YAMLError as e:
                self.fail(f"giro {giro}: file CORROTTO da scritture concorrenti: "
                          f"{e}\n---\n{testo[-300:]}")
            self.assertIsInstance(d, dict, f"giro {giro}: il file non è una mappa")
            self.assertEqual(d.get("id"), 6, f"giro {giro}: id perso")
            # Il contenuto è di UNO dei due scrittori, non un ibrido: se fosse
            # un miscuglio, `updated_at` potrebbe comparire due volte — cosa che
            # yaml accetterebbe silenziosamente tenendo l'ultima.
            self.assertEqual(testo.count("\nupdated_at:"), 1,
                             f"giro {giro}: `updated_at` compare più di una volta")
        self.assertEqual(errori, [])

    def test_la_dinamica_del_guasto_e_questa_e_write_non_la_subisce(self):
        """Ricostruisce il guasto in modo DETERMINISTICO, e mostra che `_write`
        non lo subisce.

        Due `open('w')` hanno offset indipendenti: qui li si apre entrambi
        (entrambi troncano), poi scrive il corto e infine il lungo. Con la
        vecchia implementazione il file finisce con la coda del lungo attaccata
        al corpo del corto — il `:00'` orfano dell'istanza. Con `_write` no,
        perché ogni scrittore lavora su un temporaneo suo e la sostituzione è
        atomica.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        dst = self.dir / "6.yaml"
        corto = "id: 6\nname: corto\nupdated_at: 'A'\n"
        lungo = "id: 6\nname: lungo\nupdated_at: 'B'\nextra: " + "y" * 200 + "\n"

        # --- la vecchia strada, riprodotta qui e non importata: due handle
        #     aperti insieme, scritti in ordine inverso alla lunghezza.
        with open(dst, "w", encoding="utf-8") as f_corto, \
                open(dst, "w", encoding="utf-8") as f_lungo:
            f_corto.write(corto)
            f_corto.flush()
            f_lungo.write(lungo)
            f_lungo.flush()
        misto = dst.read_text(encoding="utf-8")
        self.assertGreater(
            len(misto), len(corto),
            "la dinamica non si è prodotta: senza il residuo questo test non "
            "dimostra nulla, va riscritto invece di lasciarlo passare")
        self.assertIn("extra:", misto)

        # --- la strada nuova: gli stessi due scrittori, via `_write`.
        dst.unlink()
        a = self._job("corto")
        b = self._job("lungo " + "y" * 400)
        for _ in range(30):
            t1 = threading.Thread(target=self.db._write, args=(a,))
            t2 = threading.Thread(target=self.db._write, args=(b,))
            t1.start(); t2.start(); t1.join(); t2.join()
            testo = dst.read_text(encoding="utf-8")
            yaml.safe_load(testo)   # solleva se corrotto
            self.assertEqual(testo.count("\nupdated_at:"), 1)

    def test_un_file_illeggibile_non_esce_in_silenzio(self):
        """L'altra metà del guasto: il job spariva senza una riga di log. Deve
        restare `None` — un job illeggibile non si inventa — ma detto."""
        self.dir.mkdir(parents=True, exist_ok=True)
        rotto = self.dir / "6.yaml"
        rotto.write_text("id: 6\nname: x\n:00'\nupdated_at: 'y'\n", encoding="utf-8")
        with self.assertLogs("scheduler.db", level="ERROR") as log:
            self.assertIsNone(self.db._read(rotto))
        self.assertTrue(
            any("6.yaml" in r for r in log.output),
            f"il log non nomina il file: {log.output}")

    def test_il_job_illeggibile_non_fa_sparire_gli_altri(self):
        """`_all` deve saltare il rotto e restituire i sani: un file corrotto
        non è una ragione per perdere l'elenco."""
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db._write(self._job("ok"))
        (self.dir / "7.yaml").write_text("id: 7\n:00'\n", encoding="utf-8")
        with self.assertLogs("scheduler.db", level="ERROR"):
            ids = [j["id"] for j in self.db._all()]
        self.assertEqual(ids, [6])

    def test_nessun_temporaneo_resta_nella_cartella(self):
        """Il temporaneo vive fra la scrittura e il `replace`: se restasse,
        sporcherebbe la cartella dei dati di chi ha clonato la piattaforma."""
        self.db._write(self._job("ok"))
        residui = [p.name for p in self.dir.iterdir() if ".tmp" in p.name]
        self.assertEqual(residui, [], f"temporanei rimasti: {residui}")


if __name__ == "__main__":
    unittest.main()
