"""Un run dichiara com'è andata; «il turno è finito» non è una risposta.

Prima di questa modifica lo stato di un run agentico era il valore di verità di
`await chat.send_user_message(prompt)`. Il caso misurato che la motiva
(clodia-platform#206, 22 ago 2026): il job «Daily digest GRC» ha girato 652
secondi, ha tentato `email.send` tre volte, ha fallito tre volte — il refresh
OAuth della casella rispondeva `invalid_grant` — e ha registrato **`success`**.

I test qui sotto sono scritti sul comportamento, non sull'implementazione: il
primo (`test_il_caso_del_digest_grc`) è precisamente quello scenario, e su `main`
sarebbe verde con lo stato sbagliato.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from . import db, run_status, scheduler


class _Chat:
    """Sessione finta: espone il solo `chat_id` e un turno che si comporta come
    chiesto. Non imita l'SDK — il turno vero è fuori dal confine di questo test."""

    def __init__(self, chat_id: str, *, solleva: Exception | None = None,
                 durante=None):
        self.chat_id = chat_id
        self._solleva = solleva
        self._durante = durante

    async def send_user_message(self, prompt: str) -> str:
        if self._durante is not None:
            self._durante()
        if self._solleva is not None:
            raise self._solleva
        return "fatto"


class _JobFinto:
    """Storico run in memoria, con la stessa forma di quello su disco."""

    def __init__(self):
        self.runs: dict[str, dict] = {}
        self.seq = 0

    def mark(self) -> str:
        self.seq += 1
        rid = str(self.seq)
        self.runs[rid] = {"id": rid, "stato": "running", "error": None}
        return rid

    def complete(self, job_id, run_id, *, status=None, success=None, error=None):
        # Replica la sola regola che il test deve poter osservare, incluse le due
        # guardie sui parametri: un test che accettasse qualunque combinazione non
        # accorgerebbe della loro rimozione.
        if status is None and success is None:
            raise ValueError("richiede status oppure success")
        if status is not None and success is not None:
            raise ValueError("status OPPURE success")
        if status is None:
            status = "success" if success else "failed"
        if status not in db.TERMINAL_STATES:
            raise ValueError(f"stato non valido: {status}")
        row = self.runs.get(str(run_id))
        if row is None:
            return False
        det = str(error).strip() if error not in (None, "") else None
        if status in db.NOT_OK and not det:
            det = "nessun dettaglio fornito"
        row.update({"stato": status, "error": det if status in db.NOT_OK else None})
        return True


class DichiarazioneDelloStatoTests(unittest.TestCase):
    """Il registro delle dichiarazioni, isolato dal resto."""

    def setUp(self):
        for c in ("c1", "c2", "chat-x"):
            run_status.forget(c)

    def test_i_tre_stati_dichiarabili(self):
        for s in ("success", "error", "fatal"):
            with self.subTest(stato=s):
                self.assertEqual(run_status.declare("c1", s), s)
                self.assertEqual(run_status.take("c1")[0], s)

    def test_failed_non_e_dichiarabile(self):
        """Un agente che è morto non dichiara nulla: `failed` lo constata
        l'infrastruttura. Ammetterlo qui renderebbe indistinguibili le due cose."""
        with self.assertRaises(ValueError) as ctx:
            run_status.declare("c1", "failed")
        self.assertIn("failed", str(ctx.exception))

    def test_uno_stato_inventato_e_rifiutato_con_i_valori_validi(self):
        with self.assertRaises(ValueError) as ctx:
            run_status.declare("c1", "quasi-ok")
        msg = str(ctx.exception)
        for s in ("success", "error", "fatal"):
            self.assertIn(s, msg, "l'errore deve elencare i valori ammessi")

    def test_senza_dichiarazione_lo_stato_e_error_non_success(self):
        stato, dettaglio = run_status.take("mai-visto")
        self.assertEqual(stato, "error")
        self.assertNotEqual(stato, "success")
        self.assertIn("non ha dichiarato", dettaglio or "")

    def test_la_dichiarazione_si_consuma(self):
        """Vale per UN run: lasciarla in memoria la farebbe ereditare al run
        successivo dello stesso job."""
        run_status.declare("c1", "fatal", "niente da consegnare")
        self.assertEqual(run_status.take("c1")[0], "fatal")
        self.assertEqual(run_status.take("c1")[0], "error", "seconda lettura: non ereditata")

    def test_l_ultima_dichiarazione_vince(self):
        run_status.declare("c1", "fatal", "sembrava perso")
        run_status.declare("c1", "error", "recuperate 2 fonti su 5")
        stato, dettaglio = run_status.take("c1")
        self.assertEqual(stato, "error")
        self.assertIn("2 fonti", dettaglio or "")

    def test_le_dichiarazioni_non_si_mescolano_fra_turni(self):
        run_status.declare("c1", "fatal")
        run_status.declare("c2", "success")
        self.assertEqual(run_status.take("c2")[0], "success")
        self.assertEqual(run_status.take("c1")[0], "fatal")

    def test_chat_id_vuoto_e_rifiutato(self):
        with self.assertRaises(ValueError):
            run_status.declare("", "success")


class StatoTerminaleDelRunTests(unittest.IsolatedAsyncioTestCase):
    """`_complete_agentic_run`: cosa finisce nello storico."""

    def setUp(self):
        self.job = _JobFinto()
        for c in ("chat-grc", "chat-ok", "chat-morto", "chat-parziale"):
            run_status.forget(c)

    async def _esegui(self, chat) -> dict:
        rid = self.job.mark()
        with patch.object(scheduler.db, "complete_run", self.job.complete):
            await scheduler._complete_agentic_run(1, rid, chat, "prompt")
        return self.job.runs[rid]

    async def test_il_caso_del_digest_grc(self):
        """Il turno finisce senza sollevare, l'agente NON dichiara nulla perché
        l'invio è fallito: su `main` questo run è `success`. È il difetto."""
        row = await self._esegui(_Chat("chat-grc"))
        self.assertEqual(row["stato"], "error")
        self.assertNotEqual(row["stato"], "success",
                            "652 secondi di lavoro e nessuna email: non è un successo")
        self.assertIn("non ha dichiarato", row["error"] or "")

    async def test_un_run_che_dichiara_success(self):
        row = await self._esegui(_Chat("chat-ok", durante=lambda: run_status.declare(
            "chat-ok", "success")))
        self.assertEqual(row["stato"], "success")
        self.assertIsNone(row["error"])

    async def test_un_run_che_consegna_con_riserva_e_error(self):
        """`error` = il lavoro è stato fatto, ma la qualità può esserne
        compromessa. Il dettaglio è la parte utile e deve arrivare a destinazione."""
        row = await self._esegui(_Chat(
            "chat-parziale",
            durante=lambda: run_status.declare(
                "chat-parziale", "error", "3 fonti su 5 in 403: digest con 2 voci")))
        self.assertEqual(row["stato"], "error")
        self.assertIn("3 fonti su 5", row["error"] or "")

    async def test_un_run_che_non_ha_prodotto_nulla_e_fatal(self):
        row = await self._esegui(_Chat(
            "chat-ok",
            durante=lambda: run_status.declare("chat-ok", "fatal", "nessuna fonte raggiungibile")))
        self.assertEqual(row["stato"], "fatal")
        self.assertIn("nessuna fonte", row["error"] or "")

    async def test_il_turno_che_muore_resta_failed(self):
        """`failed` e `fatal` non si sovrappongono: qui l'infrastruttura constata,
        non l'agente."""
        row = await self._esegui(_Chat("chat-morto", solleva=RuntimeError("provider giù")))
        self.assertEqual(row["stato"], "failed")
        self.assertIn("provider giù", row["error"] or "")

    async def test_una_dichiarazione_non_sopravvive_al_turno_morto(self):
        """L'agente dichiara `success`, POI il turno muore. Se la dichiarazione
        restasse in memoria, il PROSSIMO run dello stesso job la leggerebbe e si
        registrerebbe come riuscito senza aver fatto niente."""
        def dichiara_poi_muori():
            run_status.declare("chat-morto", "success")
            raise RuntimeError("morto dopo aver dichiarato")

        chat = _Chat("chat-morto")
        chat.send_user_message = lambda _p: _solleva_async(dichiara_poi_muori)
        rid = self.job.mark()
        with patch.object(scheduler.db, "complete_run", self.job.complete):
            await scheduler._complete_agentic_run(1, rid, chat, "prompt")
        self.assertEqual(self.job.runs[rid]["stato"], "failed")
        self.assertIsNone(run_status.peek("chat-morto"),
                          "la dichiarazione del turno morto va scartata")


async def _solleva_async(fn):
    fn()


class StatiTerminaliTests(unittest.TestCase):
    """Il contratto verso la UI, che su questi valori dipinge un pallino."""

    def test_i_quattro_stati_terminali(self):
        self.assertEqual(set(db.TERMINAL_STATES), {"success", "error", "fatal", "failed"})

    def test_success_e_l_unico_stato_ok(self):
        self.assertEqual(set(db.NOT_OK), set(db.TERMINAL_STATES) - {"success"})

    def test_ogni_stato_dichiarabile_e_anche_terminale(self):
        """Se un agente potesse dichiarare uno stato che lo storico non accetta,
        la dichiarazione morirebbe in un ValueError al completamento — cioè un
        run legittimo diventerebbe un errore di programmazione."""
        for s in run_status.DECLARABLE:
            with self.subTest(stato=s):
                self.assertIn(s, db.TERMINAL_STATES)


if __name__ == "__main__":
    unittest.main()
