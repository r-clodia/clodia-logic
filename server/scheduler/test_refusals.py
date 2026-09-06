"""Un verbo negato durante il run arriva nel run record, col suo nome.

Residuo di clodia-platform#206 dopo `#341`. Quella modifica ha già togliato il
difetto peggiore — un run che non dichiara nulla è `error`, non `success` — ma
lascia scoperte due cose, ed è ciò che questi test misurano:

1. un `success` DICHIARATO copre un rifiuto incassato. `db.complete_run` scarta
   il dettaglio quando lo stato è `success` (`error` è valorizzato solo per
   `NOT_OK`), quindi il rifiuto non lascia traccia da nessuna parte: è la stessa
   forma autoconfermante della issue, un gradino più in alto;
2. nessun run record nomina MAI il verbo. «l'agente non ha dichiarato l'esito» è
   vero e inutile: il fatto che serve a chi legge è `email.send negato`.

I test sono scritti sul comportamento. Il primo — `test_un_success_dichiarato_
non_copre_un_verbo_negato` — è rosso su `main`.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import db, refusals, run_status, scheduler
from .test_run_exit_status import _Chat, _JobFinto


class IlRegistroDeiRifiutiTests(unittest.TestCase):
    """Il registro, isolato dal resto. Gemello di `run_status`: vive per UN
    turno e si consuma."""

    def setUp(self):
        for c in ("c1", "c2"):
            refusals.forget(c)

    def test_un_rifiuto_registrato_si_rilegge_col_verbo_e_la_classe(self):
        refusals.note("c1", "email.send", "denied_tools")
        letti = refusals.take("c1")
        self.assertEqual([(r["verb"], r["why"]) for r in letti],
                         [("email.send", "denied_tools")])

    def test_senza_rifiuti_la_lettura_e_una_lista_vuota(self):
        """Non `None`: un chiamante che testa la verità di un oggetto qualunque
        finirebbe per far fallire ogni run dei runtime che il registro non
        alimentano."""
        self.assertEqual(refusals.take("mai-visto"), [])

    def test_la_lettura_consuma(self):
        """Vale per UN run. Lasciarlo lì farebbe fallire il run successivo dello
        stesso job per un rifiuto di ieri."""
        refusals.note("c1", "web.fetch", "whitelist")
        self.assertEqual(len(refusals.take("c1")), 1)
        self.assertEqual(refusals.take("c1"), [], "seconda lettura: non ereditata")

    def test_i_turni_non_si_mescolano(self):
        refusals.note("c1", "email.send", "denied_tools")
        refusals.note("c2", "shell.exec", "clearance")
        self.assertEqual(refusals.take("c2")[0]["verb"], "shell.exec")
        self.assertEqual(refusals.take("c1")[0]["verb"], "email.send")

    def test_forget_scarta_i_pendenti(self):
        refusals.note("c1", "email.send", "denied_tools")
        refusals.forget("c1")
        self.assertEqual(refusals.take("c1"), [])

    def test_un_verbo_senza_nome_non_si_registra(self):
        """Una riga `verbo negato: ''` degraderebbe il dettaglio del run a
        rumore proprio nel punto in cui deve nominare qualcosa."""
        with self.assertRaises(ValueError):
            refusals.note("c1", "  ", "denied_tools")
        with self.assertRaises(ValueError):
            refusals.note("", "email.send", "denied_tools")

    def test_il_riassunto_conta_i_tentativi_invece_di_ripeterli(self):
        """Il caso vero: tre `email.send` di fila (job 4, run 28). Tre righe
        identiche nel dettaglio non dicono nulla in più di `×3`."""
        for _ in range(3):
            refusals.note("c1", "email.send", "denied_tools")
        testo = refusals.summary(refusals.take("c1"))
        self.assertEqual(testo.count("email.send"), 1)
        self.assertIn("×3", testo)
        self.assertIn("denied_tools", testo)

    def test_il_riassunto_nomina_ogni_verbo_distinto(self):
        refusals.note("c1", "email.send", "denied_tools")
        refusals.note("c1", "web.fetch", "whitelist")
        testo = refusals.summary(refusals.take("c1"))
        self.assertIn("email.send", testo)
        self.assertIn("web.fetch", testo)

    def test_un_rifiuto_senza_classe_resta_leggibile(self):
        """La classe è un `detail` opzionale lato gateway: se manca, il verbo
        vale comunque più del silenzio."""
        refusals.note("c1", "email.send")
        self.assertIn("email.send", refusals.summary(refusals.take("c1")))


class IlRifiutoNelRunRecordTests(unittest.IsolatedAsyncioTestCase):
    """`_complete_agentic_run`: cosa finisce nello storico quando un verbo è
    stato negato dentro il turno."""

    def setUp(self):
        self.job = _JobFinto()
        for c in ("chat-grc", "chat-ok", "chat-morto", "chat-vecchia"):
            run_status.forget(c)
            refusals.forget(c)

    async def _esegui(self, chat) -> dict:
        rid = self.job.mark()
        with patch.object(scheduler.db, "complete_run", self.job.complete):
            await scheduler._complete_agentic_run(1, rid, chat, "prompt")
        return self.job.runs[rid]

    async def test_un_success_dichiarato_non_copre_un_verbo_negato(self):
        """Il caso che resta aperto dopo #341: l'agente incassa il diniego, poi
        dichiara `success` — per ottimismo o per errore — e il run è verde.

        Rosso su `main`: 'success' != 'error'."""
        def durante():
            refusals.note("chat-grc", "email.send", "denied_tools")
            run_status.declare("chat-grc", "success")

        row = await self._esegui(_Chat("chat-grc", durante=durante))
        self.assertEqual(row["stato"], "error")
        self.assertIn("email.send", row["error"] or "",
                      "il dettaglio deve nominare il verbo negato")

    async def test_il_run_non_dichiarato_nomina_il_verbo_invece_della_frase(self):
        """Su `main` questo run è già `error`, ma il dettaglio dice solo che
        l'agente non ha parlato. Il fatto utile è quale verbo gli è mancato."""
        row = await self._esegui(_Chat(
            "chat-grc",
            durante=lambda: refusals.note("chat-grc", "email.send", "denied_tools")))
        self.assertEqual(row["stato"], "error")
        self.assertIn("email.send", row["error"] or "")

    async def test_la_dichiarazione_dell_agente_non_viene_sovrascritta(self):
        """`fatal` dice più di `error`: il turno è arrivato in fondo e il lavoro
        non è stato fatto, e lo sa l'agente. Declassarlo a `error` perché c'è un
        rifiuto perderebbe informazione invece di aggiungerla."""
        def durante():
            refusals.note("chat-ok", "email.send", "denied_tools")
            run_status.declare("chat-ok", "fatal", "nessun digest consegnato")

        row = await self._esegui(_Chat("chat-ok", durante=durante))
        self.assertEqual(row["stato"], "fatal")
        self.assertIn("nessun digest", row["error"] or "",
                      "le parole dell'agente restano")
        self.assertIn("email.send", row["error"] or "",
                      "e il fatto misurato si aggiunge")

    async def test_senza_rifiuti_un_success_resta_success(self):
        """Il verso opposto dello stesso errore: un registro letto male farebbe
        nascere `error` su ogni run pulito."""
        row = await self._esegui(_Chat(
            "chat-ok", durante=lambda: run_status.declare("chat-ok", "success")))
        self.assertEqual(row["stato"], "success")
        self.assertIsNone(row["error"])

    async def test_un_rifiuto_del_turno_precedente_non_fa_fallire_questo_run(self):
        """La stessa chat serve più run. Un diniego incassato ieri — o in una
        conversazione interattiva sulla stessa sessione — non descrive il lavoro
        di oggi."""
        refusals.note("chat-vecchia", "email.send", "denied_tools")
        row = await self._esegui(_Chat(
            "chat-vecchia",
            durante=lambda: run_status.declare("chat-vecchia", "success")))
        self.assertEqual(row["stato"], "success")

    async def test_il_turno_morto_resta_failed_e_non_lascia_residui(self):
        """`failed` lo constata l'infrastruttura e non si discute: il turno non
        è arrivato in fondo. Ma i rifiuti pendenti vanno scartati, o li leggerà
        il run successivo."""
        def durante():
            refusals.note("chat-morto", "email.send", "denied_tools")

        row = await self._esegui(_Chat("chat-morto", durante=durante,
                                       solleva=RuntimeError("provider giù")))
        self.assertEqual(row["stato"], "failed")
        self.assertEqual(refusals.peek("chat-morto"), [],
                         "un rifiuto del turno morto non appartiene al prossimo run")


class LaRottaCheRiceveIRifiutiDalGatewayTests(unittest.TestCase):
    """`POST /clodia/jobs/refusal/internal`: la sorgente che NON decidiamo noi.

    Il gateway calcola già verbo e classe del motivo nel ramo `PermissionError`
    del proprio dispatch; questa rotta è l'unica cosa che mancava perché quel
    fatto arrivasse al run record."""

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from . import api

        app = FastAPI()
        app.include_router(api.router)
        self.client = TestClient(app)
        refusals.forget("chat-gw")

    def tearDown(self):
        refusals.forget("chat-gw")

    def test_un_diniego_del_gateway_arriva_nel_registro(self):
        r = self.client.post("/clodia/jobs/refusal/internal",
                             json={"chat_id": "chat-gw", "verb": "email.send",
                                   "why": "denied_tools"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([(x["verb"], x["why"]) for x in refusals.peek("chat-gw")],
                         [("email.send", "denied_tools")])

    def test_senza_verbo_e_400_e_non_registra(self):
        r = self.client.post("/clodia/jobs/refusal/internal",
                             json={"chat_id": "chat-gw", "why": "denied_tools"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(refusals.peek("chat-gw"), [])

    def test_senza_chat_e_400(self):
        r = self.client.post("/clodia/jobs/refusal/internal",
                             json={"verb": "email.send"})
        self.assertEqual(r.status_code, 400)

    def test_la_classe_del_motivo_e_opzionale(self):
        r = self.client.post("/clodia/jobs/refusal/internal",
                             json={"chat_id": "chat-gw", "verb": "email.send"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(refusals.peek("chat-gw")[0]["verb"], "email.send")


class IlGateNativoRegistraCioCheNegaTests(unittest.TestCase):
    """L'altra sorgente: il diniego sui `native_tools` lo decidiamo noi, in
    `_permission_gate`. Se non lo registrasse lì, non lo saprebbe nessun altro —
    quel rifiuto non passa dal gateway."""

    def setUp(self):
        refusals.forget("chat-gate")

    def tearDown(self):
        refusals.forget("chat-gate")

    def _chiedi(self, negati, nome, chat_id="chat-gate"):
        import asyncio

        from ..sdk_runtime import session as S
        return asyncio.run(S._permission_gate(negati, chat_id)(nome, {}, None))

    def test_un_tool_negato_lascia_una_riga_col_suo_nome(self):
        from claude_agent_sdk import PermissionResultDeny
        esito = self._chiedi(["WebSearch"], "WebSearch")
        self.assertIsInstance(esito, PermissionResultDeny)
        self.assertEqual([(x["verb"], x["why"]) for x in refusals.peek("chat-gate")],
                         [("WebSearch", refusals.WHY_NATIVE_TOOLS)])

    def test_cio_che_passa_non_lascia_nulla(self):
        self._chiedi(["WebSearch"], "Read")
        self.assertEqual(refusals.peek("chat-gate"), [])

    def test_senza_chat_id_il_gate_nega_lo_stesso(self):
        """Il gate serve fuori dai job (chat interattive incluse): registrare è
        un di più, negare è il suo lavoro."""
        from claude_agent_sdk import PermissionResultDeny
        self.assertIsInstance(self._chiedi(["WebSearch"], "WebSearch", ""),
                              PermissionResultDeny)
        self.assertEqual(refusals.peek(""), [])


class IlDettaglioArrivaAChiLeggeTests(unittest.TestCase):
    """Il dettaglio esiste per essere letto: `complete_run` lo persiste solo per
    gli stati `NOT_OK`, ed è la ragione per cui il declassamento non è
    cosmetico."""

    def test_su_success_il_dettaglio_non_viene_persistito(self):
        self.assertNotIn("success", db.NOT_OK)


if __name__ == "__main__":
    unittest.main()
