"""Lo storico del ragionamento: dove si scrive, quanto tiene, cosa butta.

clodia-platform#484. Il ragionamento di un turno era live-only: chi riapriva il
topic a turno finito non aveva nessun modo di vederlo. Qui si verifica il
deposito — non la pipeline di emissione, che sta in `sdk_runtime`.

La prima proprietà è di CONTENIMENTO, non di comodità: l'activity log esistente
è indicizzato per agente e **non sa in che tier è successo** (`agent-state/
activity/<agent>/YYYY-MM-DD.jsonl`). Il ragionamento di un turno in SEAL-4 e
quello di un turno in SEAL-0 finirebbero nello stesso file, e il ragionamento
cita il contenuto del canale. Per questo lo store è separato e la prima cosa
che il path nomina è il tier.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from . import reasoning_log


class _ConStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.radice = Path(self._tmp.name) / "reasoning"
        self._p = patch.object(reasoning_log, "REASONING_DIR", self.radice)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(self._tmp.cleanup)


class IlPathNominaIlTier(_ConStore):
    """Il contenimento è nella struttura, non in un campo dentro il file."""

    def test_il_tier_e_la_prima_cartella(self) -> None:
        reasoning_log.record("SEAL-2", "preventivi-tomato", message_id="m1",
                             spawn="clodia-7", seed="clodia", text="ci penso")
        scritti = sorted(p.relative_to(self.radice).as_posix()
                         for p in self.radice.rglob("*.jsonl"))
        self.assertEqual(1, len(scritti))
        self.assertTrue(scritti[0].startswith("SEAL-2/preventivi-tomato/"),
                        f"il tier non apre il path: {scritti[0]}")

    def test_due_tier_non_condividono_nessun_file(self) -> None:
        """Il difetto che si sta evitando: un solo file per agente."""
        reasoning_log.record("SEAL-0", "pubblico", message_id="a",
                             spawn="clodia-1", seed="clodia", text="banale")
        reasoning_log.record("SEAL-4", "riservato", message_id="b",
                             spawn="clodia-2", seed="clodia", text="delicato")
        for p in self.radice.rglob("*.jsonl"):
            testo = p.read_text(encoding="utf-8")
            self.assertFalse("banale" in testo and "delicato" in testo,
                             "due tier nello stesso file")

    def test_un_nome_che_esce_dalla_cartella_viene_rifiutato(self) -> None:
        for tier, name in (("../etc", "x"), ("SEAL-1", "../../fuori"),
                           ("SEAL-1", "a/b"), ("", "x")):
            with self.subTest(tier=tier, name=name):
                with self.assertRaises(ValueError):
                    reasoning_log.record(tier, name, message_id="m",
                                         spawn="s", seed="s", text="t")


class IlTettoPerTurno(_ConStore):
    """64 KB per turno: 32k di testa e 32k di coda.

    Il ragionamento è verboso e il tetto serve, ma troncare SOLO in coda
    butterebbe la conclusione — che è la parte per cui lo si va a rileggere — e
    troncare solo in testa butterebbe l'impostazione del problema. Si tengono i
    due capi e si dichiara quanto manca in mezzo.
    """

    def test_sotto_il_tetto_il_testo_e_intatto(self) -> None:
        testo = "x" * 1000
        self.assertEqual((testo, False), reasoning_log.cap(testo))

    def test_esattamente_al_tetto_non_si_tronca(self) -> None:
        testo = "x" * (reasoning_log.TESTA + reasoning_log.CODA)
        self.assertEqual((testo, False), reasoning_log.cap(testo))

    def test_sopra_il_tetto_restano_i_due_capi(self) -> None:
        testo = "A" * reasoning_log.TESTA + "M" * 5000 + "Z" * reasoning_log.CODA
        tagliato, troncato = reasoning_log.cap(testo)
        self.assertTrue(troncato)
        self.assertTrue(tagliato.startswith("A" * 100))
        self.assertTrue(tagliato.endswith("Z" * 100))
        self.assertNotIn("M", tagliato, "la parte omessa è il MEZZO")
        self.assertIn("5000", tagliato, "il taglio va dichiarato, non nascosto")

    def test_il_record_scritto_rispetta_il_tetto(self) -> None:
        reasoning_log.record("SEAL-1", "c", message_id="m", spawn="s", seed="s",
                             text="A" * 200_000)
        voce = reasoning_log.read("SEAL-1", "c", "m")
        self.assertTrue(voce["truncated"])
        self.assertLessEqual(
            len(voce["text"]),
            reasoning_log.TESTA + reasoning_log.CODA + 200,
            "il tetto non vale per ciò che finisce su disco")


class SiRileggePerMessaggio(_ConStore):
    """L'aggancio è l'id del MESSAGGIO, non il turno: è la bolla che si guarda."""

    def test_scrivi_e_rileggi(self) -> None:
        reasoning_log.record("SEAL-1", "c", message_id="m1", spawn="clodia-7",
                             seed="clodia", text="prima penso, poi scrivo")
        voce = reasoning_log.read("SEAL-1", "c", "m1")
        self.assertEqual("prima penso, poi scrivo", voce["text"])
        self.assertEqual("clodia-7", voce["spawn"])
        self.assertEqual("m1", voce["message_id"])

    def test_un_messaggio_senza_ragionamento_non_esiste(self) -> None:
        self.assertIsNone(reasoning_log.read("SEAL-1", "c", "mai-visto"))

    def test_lindice_elenca_solo_i_messaggi_che_hanno_qualcosa(self) -> None:
        """Niente bolle fantasma: l'indice è ciò che accende il bottone 💭 in
        UI, e un bottone che apre il vuoto è peggio di nessun bottone."""
        reasoning_log.record("SEAL-1", "c", message_id="m1", spawn="s",
                             seed="s", text="qualcosa")
        self.assertEqual(["m1"], reasoning_log.index("SEAL-1", "c"))

    def test_il_testo_vuoto_non_viene_registrato(self) -> None:
        reasoning_log.record("SEAL-1", "c", message_id="m1", spawn="s",
                             seed="s", text="   \n ")
        self.assertEqual([], reasoning_log.index("SEAL-1", "c"))

    def test_lindice_di_un_canale_non_vede_laltro(self) -> None:
        reasoning_log.record("SEAL-1", "uno", message_id="m1", spawn="s",
                             seed="s", text="t")
        reasoning_log.record("SEAL-1", "due", message_id="m2", spawn="s",
                             seed="s", text="t")
        self.assertEqual(["m1"], reasoning_log.index("SEAL-1", "uno"))
        self.assertIsNone(reasoning_log.read("SEAL-1", "uno", "m2"))

    def test_una_riga_corrotta_non_fa_cadere_la_lettura(self) -> None:
        reasoning_log.record("SEAL-1", "c", message_id="m1", spawn="s",
                             seed="s", text="buono")
        f = next(self.radice.rglob("*.jsonl"))
        with f.open("a", encoding="utf-8") as fh:
            fh.write("{non json\n")
        self.assertEqual(["m1"], reasoning_log.index("SEAL-1", "c"))


class LaRetention(_ConStore):
    """90 giorni, per tier. Il ragionamento è il dato più voluminoso che la
    piattaforma scriva per turno: senza scadenza lo store cresce per sempre."""

    def _file_vecchio(self, tier: str, giorni: int) -> Path:
        quando = datetime.now(timezone.utc) - timedelta(days=giorni)
        p = (self.radice / tier / "c" / f"{quando.strftime('%Y-%m-%d')}.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"message_id": "vecchio", "text": "t"}) + "\n",
                     encoding="utf-8")
        return p

    def test_oltre_i_90_giorni_sparisce(self) -> None:
        vecchio = self._file_vecchio("SEAL-1", 91)
        recente = self._file_vecchio("SEAL-1", 89)
        reasoning_log.purge()
        self.assertFalse(vecchio.exists())
        self.assertTrue(recente.exists())

    def test_la_scadenza_vale_in_ogni_tier(self) -> None:
        vecchi = [self._file_vecchio(t, 200) for t in ("SEAL-0", "SEAL-3")]
        reasoning_log.purge()
        for v in vecchi:
            self.assertFalse(v.exists(), f"{v} sopravvissuto alla scadenza")

    def test_un_file_con_nome_non_datato_resta(self) -> None:
        """Non si cancella ciò che non si sa datare: è il modo in cui una
        pulizia diventa una perdita."""
        strano = self.radice / "SEAL-1" / "c" / "appunti.jsonl"
        strano.parent.mkdir(parents=True, exist_ok=True)
        strano.write_text("{}\n", encoding="utf-8")
        reasoning_log.purge()
        self.assertTrue(strano.exists())

    def test_il_primo_scritto_del_giorno_fa_pulizia(self) -> None:
        """La scadenza non ha bisogno di un task suo: il trigger naturale è la
        nascita del file di oggi, che capita una volta al giorno."""
        vecchio = self._file_vecchio("SEAL-1", 400)
        reasoning_log.record("SEAL-1", "c", message_id="m", spawn="s",
                             seed="s", text="oggi")
        self.assertFalse(vecchio.exists())


class NonFiniscePiuNellActivityLog(unittest.TestCase):
    """La docstring dell'activity log prometteva `thinking_chunk` fra le sue
    estensioni future. Era la strada sbagliata, ed è la ragione per cui questa
    issue poteva essere implementata male: quel file è indicizzato per AGENTE e
    non sa in che tier sta scrivendo."""

    def test_lactivity_log_non_promette_piu_il_ragionamento(self) -> None:
        from . import activity_log
        doc = activity_log.__doc__ or ""
        self.assertNotIn("Future estensioni", doc,
                         "il ragionamento non è un'estensione futura di questo "
                         "file: è una strada chiusa, e va detto")
        self.assertIn("reasoning_log", doc,
                      "chi legge qui deve trovare dov'è finito il ragionamento")
        self.assertIn("tier", doc, "va detto PERCHÉ non sta qui")


if __name__ == "__main__":
    unittest.main()
