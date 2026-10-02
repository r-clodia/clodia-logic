"""clodia-platform#473 · il blocco del loop e la sessione opencode caduta si leggono insieme.

La notte 30/9→1/10 l'event loop è rimasto fermo 20824s e poi 522s. Un'ora dopo
il turno di `segretario-38` ha trovato la sua sessione opencode invalida
(HTTP 404 → ricreo) e ha bruciato i 180s pieni del budget residuo, uscendo con

    OpenCodeTurnTimeout('turno opencode scaduto (dopo ricreazione della
    sessione): attesi 180s sul budget di 180s — modello gemma-4-26b-a4b-it')

Quel messaggio è vero e non serve a niente: dice «lento», e chi lo legge conclude
«provider». La issue elenca il difetto fra i **gap degli strumenti**, con due
richieste testuali:

- «Nessuna correlazione automatica fra `loop_lag` ed eventuali sessioni opencode
  invalidate nella stessa finestra: andrebbero loggate come causa-effetto
  esplicita, non dedotte a mano dal timestamp.»
- «Il messaggio d'errore esposto non distingue "provider lento" da "sessione
  appena ricreata dopo un'interruzione di piattaforma".»

Ricostruire quella correlazione a mano è costato un'indagine sui log; il dato
era già in casa (`core.loop_lag` registra gli stalli dal #358) e non lo leggeva
nessuno su questo percorso.

Quello che qui NON si afferma: che lo stallo sia la CAUSA. Si dichiara ciò che
si è osservato — «il loop è rimasto bloccato Xs nella finestra» — e si lascia la
conclusione a chi indaga. È la stessa regola per cui «modello non convergente» è
stato tolto in #423: una diagnosi scritta al posto di un'osservazione manda la
ricerca dalla parte sbagliata, e l'ha già fatto due volte.
"""
from __future__ import annotations

import logging
import pathlib
import tempfile
import unittest
from unittest import mock

from . import session as S
from ..core import loop_lag
from .test_423_budget_turno import APPESO, _500_SESSIONE_MORTA, _Serve, _sessione

#: Lo stallo vero della notte del 30/9.
_STALLO = 20824.0


class StalliRecentiTests(unittest.TestCase):
    """`recent_stall` guarda indietro di una finestra, non dall'inizio dei tempi."""

    def setUp(self) -> None:
        loop_lag.reset()
        self.addCleanup(loop_lag.reset)

    def test_uno_stallo_dentro_la_finestra_si_vede(self) -> None:
        loop_lag.note_stall(1000.0, _STALLO)
        self.assertEqual(_STALLO, loop_lag.recent_stall(3600.0, clock=lambda: 1200.0))

    def test_uno_stallo_piu_vecchio_della_finestra_non_si_attribuisce(self) -> None:
        """Un blocco di ieri non spiega il turno di oggi, e attribuirglielo
        manderebbe la diagnosi successiva nel posto sbagliato."""
        loop_lag.note_stall(1000.0, _STALLO)
        self.assertEqual(0.0, loop_lag.recent_stall(60.0, clock=lambda: 5000.0))

    def test_senza_stalli_e_zero(self) -> None:
        self.assertEqual(0.0, loop_lag.recent_stall(3600.0, clock=lambda: 10.0))

    def test_la_finestra_di_default_copre_l_incidente(self) -> None:
        """Nella #473 fra l'ultimo `loop_lag` (05:58) e il 404 di segretario
        (06:52) passano 54 minuti: una finestra più corta non vedrebbe
        l'incidente che questa correlazione esiste per spiegare."""
        self.assertGreaterEqual(S._FINESTRA_CORRELAZIONE_STALLO, 54 * 60)


class _Presa(logging.Handler):
    """Raccoglie le righe del logger della sessione, zero incluso."""

    def __init__(self, dove: list) -> None:
        super().__init__(level=logging.DEBUG)
        self.dove = dove

    def emit(self, record: logging.LogRecord) -> None:
        self.dove.append(record.getMessage())


class _TurnoOpencode(unittest.IsolatedAsyncioTestCase):

    async def _turno(self, script, *, budget: float, stallo: float):
        srv = _Serve(script)
        await srv.start()
        self.addAsyncCleanup(srv.stop)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sess = _sessione(srv.port)
        # Un handler proprio invece di `assertLogs`: il caso «turno scaduto al
        # primo invio» non produce NESSUNA riga, e `assertLogs` lo tratterebbe
        # come un fallimento del test invece che come il dato da misurare.
        righe: list[str] = []
        presa = _Presa(righe)
        logger = logging.getLogger("agent-server.sdk_runtime.session")
        logger.addHandler(presa)
        self.addCleanup(logger.removeHandler, presa)
        with mock.patch.object(S, "_OPENCODE_TURN_TIMEOUT", budget), \
             mock.patch.object(S, "_OPENCODE_MIN_RETRY_BUDGET", 0.1), \
             mock.patch.object(S, "_resolve_sessions_dir",
                               return_value=pathlib.Path(d.name)), \
             mock.patch.object(S.loop_lag, "recent_stall", return_value=stallo), \
             mock.patch.object(S.activity_log, "append"):
            with self.assertRaises(RuntimeError) as ctx:
                await sess._run_turn("sposta i file di bilancio")
        return ctx.exception, "\n".join(righe)


class LaRicreazioneDiceSeIlLoopEraFermoTests(_TurnoOpencode):
    """Prima richiesta della issue: la causa-effetto nel log, non dedotta a mano."""

    async def test_la_riga_del_404_porta_lo_stallo(self) -> None:
        """Rosso prima: «sessione X inutilizzabile (HTTP 500) → ricreo» e basta,
        con lo stallo in un'altra riga di sette ore prima."""
        _err, righe = await self._turno(
            [(0.0, 500, _500_SESSIONE_MORTA), (0.0, 200, '{"id":"oc-2"}'), APPESO],
            budget=1.0, stallo=_STALLO)
        self.assertIn("inutilizzabile", righe)
        self.assertIn("20824s", righe)
        self.assertIn("event loop", righe)

    async def test_senza_stallo_la_riga_resta_quella_di_prima(self) -> None:
        """Una correlazione annunciata sempre non è una correlazione."""
        _err, righe = await self._turno(
            [(0.0, 500, _500_SESSIONE_MORTA), (0.0, 200, '{"id":"oc-2"}'), APPESO],
            budget=1.0, stallo=0.0)
        self.assertIn("inutilizzabile", righe)
        self.assertNotIn("event loop", righe)


class IlTimeoutDistingueProviderLentoDaInterruzioneTests(_TurnoOpencode):
    """Seconda richiesta della issue: la differenza sta nel messaggio esposto."""

    async def test_dopo_una_ricreazione_con_stallo_il_messaggio_lo_dice(self) -> None:
        """Rosso prima: usciva «attesi Ns sul budget di Ns — modello X», cioè la
        stessa frase che esce quando il provider è davvero lento."""
        err, _righe = await self._turno(
            [(0.0, 500, _500_SESSIONE_MORTA), (0.0, 200, '{"id":"oc-2"}'), APPESO],
            budget=1.0, stallo=_STALLO)
        testo = str(err)
        self.assertIn(S._OC_TENTATIVO_RICREATA, testo)
        self.assertIn("event loop", testo)
        self.assertIn("20824s", testo)

    async def test_non_accusa_lo_stallo_di_essere_la_causa(self) -> None:
        """Si dichiara l'osservazione; la conclusione resta di chi indaga."""
        err, _righe = await self._turno(
            [(0.0, 500, _500_SESSIONE_MORTA), (0.0, 200, '{"id":"oc-2"}'), APPESO],
            budget=1.0, stallo=_STALLO)
        self.assertIn("gemma-4-26b-a4b-it", str(err), "il modello resta un dato")
        self.assertNotIn("a causa", str(err).lower())

    async def test_un_timeout_senza_ricreazione_non_cita_lo_stallo(self) -> None:
        """Il turno scaduto al primo invio non ha niente a che vedere con una
        sessione ricreata: appiccicargli lo stallo sarebbe rumore."""
        err, _righe = await self._turno([APPESO], budget=1.0, stallo=_STALLO)
        self.assertIn(S._OC_TENTATIVO_PRIMO, str(err))
        self.assertNotIn("event loop", str(err))

    async def test_una_ricreazione_senza_stallo_resta_un_timeout_e_basta(self) -> None:
        err, _righe = await self._turno(
            [(0.0, 500, _500_SESSIONE_MORTA), (0.0, 200, '{"id":"oc-2"}'), APPESO],
            budget=1.0, stallo=0.0)
        self.assertIn(S._OC_TENTATIVO_RICREATA, str(err))
        self.assertNotIn("event loop", str(err))


if __name__ == "__main__":
    unittest.main()
