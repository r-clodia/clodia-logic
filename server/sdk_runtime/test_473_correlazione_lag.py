"""Il blocco dell'event loop si legge accanto al guasto (clodia-platform#473).

La notte 30/9→1/10 il loop resta bloccato 20824s e poi 522s; un'ora dopo la
sessione opencode di `segretario` risulta invalida (HTTP 404), viene ricreata e
il turno brucia i 180s di budget. Tre righe di log scritte da tre posti diversi,
e per capire che raccontano lo stesso incidente è servita un'indagine a mano su
due notti di log — mentre chi legge il messaggio d'errore in canale, quella
indagine, non la fa mai. La issue lo dice:

    «Nessuna correlazione automatica fra loop_lag ed eventuali sessioni opencode
     invalidate nella stessa finestra: andrebbero loggate come causa-effetto
     esplicita, non dedotte a mano dal timestamp.»

    «Il messaggio d'errore esposto (OpenCodeTurnTimeout) non distingue "provider
     lento" da "sessione appena ricreata dopo un'interruzione di piattaforma".»

`loop_lag.stall_since` già esisteva, ma risponde a un'altra domanda — «il loop
si è fermato DURANTE questa cosa» — e vuole l'istante di inizio. Una sessione
trovata invalida e un processo ucciso non ce l'hanno: si scoprono, non si
cronometrano. Da qui `ultimo_blocco`/`nota_blocco`, che guardano indietro.

Quello che la frase afferma è una CONCOMITANZA con i suoi numeri, non una causa:
il loop bloccato poco prima è compatibile con un riavvio o una sospensione della
piattaforma, e per decidere servono gli altri indizi. Dichiararla come causa
certa sarebbe l'errore che la #358 ha già pagato con sette ore cercate dalla
parte sbagliata.
"""
from __future__ import annotations

import time
import unittest

from . import session as S
from ..core import loop_lag
from .test_423_budget_turno import APPESO, _500_SESSIONE_MORTA, _TurnoBase


class UltimoBloccoTests(unittest.TestCase):
    def setUp(self) -> None:
        loop_lag.reset()
        self.addCleanup(loop_lag.reset)

    def test_senza_blocchi_non_dice_niente(self) -> None:
        """Il caso normale è il silenzio: una correlazione attaccata a ogni
        errore smetterebbe di significare qualcosa."""
        self.assertIsNone(loop_lag.ultimo_blocco(clock=lambda: 10_000.0))
        self.assertEqual("", loop_lag.nota_blocco(clock=lambda: 10_000.0))

    def test_un_blocco_recente_si_vede_con_quanto_tempo_fa(self) -> None:
        loop_lag.note_stall(fine=9_000.0, durata=20_824.0)
        durata, fa = loop_lag.ultimo_blocco(7_200.0, clock=lambda: 10_000.0)
        self.assertEqual(20_824.0, durata)
        self.assertEqual(1_000.0, fa)

    def test_un_blocco_vecchio_non_si_attribuisce(self) -> None:
        """La finestra è il confine fra una correlazione e una coincidenza."""
        loop_lag.note_stall(fine=1_000.0, durata=300.0)
        self.assertIsNone(loop_lag.ultimo_blocco(7_200.0, clock=lambda: 10_000.0))

    def test_vale_il_piu_recente(self) -> None:
        """Nella #473 i blocchi sono due, a nove minuti l'uno dall'altro: quello
        che spiega la sessione invalida è il secondo."""
        loop_lag.note_stall(fine=8_000.0, durata=20_824.0)
        loop_lag.note_stall(fine=9_500.0, durata=522.0)
        durata, _fa = loop_lag.ultimo_blocco(7_200.0, clock=lambda: 10_000.0)
        self.assertEqual(522.0, durata)

    def test_la_frase_porta_i_numeri_e_non_una_conclusione(self) -> None:
        loop_lag.note_stall(fine=9_000.0, durata=20_824.0)
        nota = loop_lag.nota_blocco(7_200.0, clock=lambda: 10_000.0)
        self.assertIn("20824", nota)
        self.assertIn("1000", nota)
        self.assertIn("può", nota, "la frase deve restare un indizio, non una causa")


class LaSessioneInvalidaCitaIlBloccoTests(_TurnoBase):
    """Il primo dei due gap: la riga «sessione inutilizzabile → ricreo» non
    diceva nulla del riavvio che l'ha resa inutilizzabile."""

    def setUp(self) -> None:
        loop_lag.reset()
        self.addCleanup(loop_lag.reset)

    async def test_la_riga_del_ricreo_dichiara_il_blocco(self) -> None:
        loop_lag.note_stall(fine=time.monotonic(), durata=20_824.0)
        with self.assertLogs("agent-server.sdk_runtime.session", "WARNING") as log:
            await self._turno([(0.0, 500, _500_SESSIONE_MORTA),
                               (0.0, 200, '{"id":"oc-2"}'),
                               APPESO],
                              budget=1.0, minimo=0.2)
        ricreo = [r for r in log.output if "inutilizzabile" in r]
        self.assertTrue(ricreo, "la riga del ricreo è sparita")
        self.assertIn("20824", ricreo[0],
                      "il blocco del loop va dedotto a mano: è il gap di #473")

    async def test_senza_blocchi_la_riga_resta_quella_di_prima(self) -> None:
        with self.assertLogs("agent-server.sdk_runtime.session", "WARNING") as log:
            await self._turno([(0.0, 500, _500_SESSIONE_MORTA),
                               (0.0, 200, '{"id":"oc-2"}'),
                               APPESO],
                              budget=1.0, minimo=0.2)
        ricreo = [r for r in log.output if "inutilizzabile" in r]
        self.assertNotIn("event loop", ricreo[0])


class IlTimeoutDistingueProviderDaPiattaformaTests(_TurnoBase):
    """Il secondo gap: letti dall'owner, «il provider è lento» e «la piattaforma
    si è interrotta e la sessione è stata rifatta» avevano lo stesso messaggio."""

    def setUp(self) -> None:
        loop_lag.reset()
        self.addCleanup(loop_lag.reset)

    async def test_il_tentativo_dopo_la_ricreazione_cita_il_blocco(self) -> None:
        loop_lag.note_stall(fine=time.monotonic(), durata=20_824.0)
        err, _s, _srv = await self._turno(
            [(0.0, 500, _500_SESSIONE_MORTA), (0.0, 200, '{"id":"oc-2"}'), APPESO],
            budget=1.0, minimo=0.2)
        self.assertIn(S._OC_TENTATIVO_RICREATA, str(err))
        self.assertIn("20824", str(err),
                      "il messaggio non distingue provider lento da piattaforma interrotta")

    async def test_il_primo_tentativo_non_la_cita(self) -> None:
        """Lì la sessione non è stata rifatta: la domanda «provider o
        piattaforma?» non è aperta, e la correlazione sarebbe rumore attaccato a
        ogni timeout."""
        loop_lag.note_stall(fine=time.monotonic(), durata=20_824.0)
        err, _s, _srv = await self._turno([APPESO], budget=1.0)
        self.assertIn(S._OC_TENTATIVO_PRIMO, str(err))
        self.assertNotIn("20824", str(err))
