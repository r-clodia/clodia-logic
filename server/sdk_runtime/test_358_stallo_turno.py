"""Un turno che muore deve dire DI COSA è morto (clodia-platform#358).

L'incidente: turno avviato alle 22:59, primo token ricevuto regolarmente, poi
silenzio per 7h34m; il watchdog l'ha chiuso alle 07:52 e nel canale è comparso
soltanto

    ⚠️ Il turno di @clodia-181 è terminato con un errore [...]
    TimeoutError()

Due difetti distinti, e conviene non confonderli.

**Il primo è che l'eccezione è muta.** `_collect_response` solleva
`asyncio.TimeoutError()` senza argomenti in due punti, e `_announce_failure`
(api/channels.py) stampa `repr(err)`: di un'eccezione senza argomenti il repr è
il nome della classe e nient'altro. La diagnosi *esiste già* un frame più sotto
— i secondi di silenzio, il cap superato — e viene buttata via da un `raise`
nudo. Non manca l'informazione: manca il passaggio.

**Il secondo è che il silenzio veniva attribuito al subprocess.** Il watchdog
scrive «nessun evento SDK da Xs», che nell'incidente è probabilmente falso: gli
eventi potevano esserci e non esserci nessuno ad ascoltarli, perché due timer
asincroni INDIPENDENTI (il watchdog a 180s e il `COLLECT_CHUNK_TIMEOUT` a 5min)
sono stati zitti insieme per sette ore — cosa che una soglia tarata male non
spiega, e un event loop bloccato sì. Un watchdog che è un task asyncio sorveglia
il prigioniero dalla cella accanto: se a bloccarsi è il thread, non gira nemmeno
lui. `core.loop_lag` misura quel blocco e glielo fa dire.

NON si tocca la soglia. Nei log di esercizio il watchdog scatta a 187s, 191s e
192s (15 set 2026 ×2, 16 set 2026): a 180s fa esattamente il suo lavoro, e
abbassarla ucciderebbe turni lenti legittimi senza cambiare nulla qui.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from . import session as S
from .test_session_recovery import _make_session


class _NeverYields:
    """Un client SDK che apre lo stream e poi non consegna mai un evento."""

    def receive_response(self):
        async def _gen():
            await asyncio.sleep(3600)
            yield None          # pragma: no cover — non ci si arriva
        return _gen()


class TimeoutSaysWhyTests(unittest.IsolatedAsyncioTestCase):
    async def test_il_silenzio_dello_stream_finisce_nel_messaggio(self) -> None:
        """Rosso prima: `repr` era `TimeoutError()`, cioè zero informazione
        proprio nel punto in cui la persona la cerca."""
        sess = _make_session()
        sess._client = _NeverYields()
        with mock.patch.object(S, "COLLECT_CHUNK_TIMEOUT", 0.01):
            with self.assertRaises(asyncio.TimeoutError) as ctx:
                await sess._collect_response()
        detto = repr(ctx.exception)
        self.assertIn("nessun evento SDK", detto)
        self.assertNotEqual("TimeoutError()", detto)

    async def test_il_cap_di_turno_dice_di_essere_il_cap(self) -> None:
        """L'altro `raise` nudo: superato `COLLECT_MAX_SECONDS`. È un guasto
        diverso dal silenzio dello stream — il turno stava anche producendo — e
        se i due si presentano con la stessa faccia la diagnosi riparte da zero."""
        sess = _make_session()
        sess._client = _NeverYields()
        with mock.patch.object(S, "COLLECT_MAX_SECONDS", 0):
            with self.assertRaises(asyncio.TimeoutError) as ctx:
                await sess._collect_response()
        self.assertIn("cap", repr(ctx.exception).lower())


class WatchdogNamesTheCulpritTests(unittest.IsolatedAsyncioTestCase):
    async def test_accusa_il_subprocess_quando_il_loop_girava(self) -> None:
        from ..core import loop_lag
        sess = _make_session()
        sess._client_ctx = mock.AsyncMock()
        sess._client = object()
        sess._last_event_at = 0.0
        turn = asyncio.create_task(asyncio.sleep(30))
        with mock.patch.object(S, "WATCHDOG_TICK", 0.01), \
             mock.patch.object(S, "WATCHDOG_SILENCE", 0.02), \
             mock.patch.object(loop_lag, "stall_since", return_value=0.0):
            await sess._turn_watchdog(turn)
        try:
            await turn
        except asyncio.CancelledError:
            pass
        self.assertIn("nessun evento SDK", sess._watchdog_reason)
        self.assertNotIn("event loop", sess._watchdog_reason)

    async def test_nomina_il_loop_bloccato_quando_c_e_stato(self) -> None:
        """Il caso della #358: il silenzio c'è, ma chi doveva ascoltare era
        fermo. Dirlo cambia da che parte si cerca il guasto la volta dopo."""
        from ..core import loop_lag
        sess = _make_session()
        sess._client_ctx = mock.AsyncMock()
        sess._client = object()
        sess._last_event_at = 0.0
        turn = asyncio.create_task(asyncio.sleep(30))
        with mock.patch.object(S, "WATCHDOG_TICK", 0.01), \
             mock.patch.object(S, "WATCHDOG_SILENCE", 0.02), \
             mock.patch.object(loop_lag, "stall_since", return_value=27276.0):
            await sess._turn_watchdog(turn)
        try:
            await turn
        except asyncio.CancelledError:
            pass
        self.assertIn("event loop", sess._watchdog_reason)
        self.assertIn("27276", sess._watchdog_reason)


class LoopLagTests(unittest.IsolatedAsyncioTestCase):
    """Il battito misura la deriva: dorme `tick` e guarda quanto è passato
    davvero. È RETROATTIVO per costruzione — mentre il loop è fermo non gira
    nessuno — e va benissimo: la riga di log compare appena il loop riparte, che
    è l'unico momento in cui qualcuno può scriverla."""

    def setUp(self) -> None:
        from ..core import loop_lag
        loop_lag.reset()

    async def test_un_battito_regolare_non_segnala_niente(self) -> None:
        from ..core import loop_lag
        orologio = iter([0.0, 1.0, 2.0, 3.0])

        async def _sleep(_s):
            pass

        await loop_lag.heartbeat(tick=1.0, threshold=30.0, giri=3,
                                 sleep=_sleep, clock=lambda: next(orologio))
        self.assertEqual(0.0, loop_lag.stall_since(0.0))

    async def test_un_salto_oltre_soglia_viene_registrato(self) -> None:
        from ..core import loop_lag
        # secondo battito: il loop riparte 7h34m dopo (27276s, il numero reale
        # dell'incidente) invece che dopo 1s.
        orologio = iter([100.0, 101.0, 27377.0])

        async def _sleep(_s):
            pass

        await loop_lag.heartbeat(tick=1.0, threshold=30.0, giri=2,
                                 sleep=_sleep, clock=lambda: next(orologio))
        bloccato = loop_lag.stall_since(100.0)
        self.assertAlmostEqual(27275.0, bloccato, places=0)

    async def test_un_blocco_vecchio_non_sporca_la_finestra_di_adesso(self) -> None:
        """Un turno iniziato DOPO che il loop è ripartito non è vittima di quel
        blocco: attribuirglielo manderebbe la diagnosi successiva nel posto
        sbagliato, che è esattamente il difetto che questa PR corregge."""
        from ..core import loop_lag
        orologio = iter([100.0, 101.0, 27377.0])

        async def _sleep(_s):
            pass

        await loop_lag.heartbeat(tick=1.0, threshold=30.0, giri=2,
                                 sleep=_sleep, clock=lambda: next(orologio))
        self.assertEqual(0.0, loop_lag.stall_since(30000.0))


class LaSogliaRestaDoveEViaTests(unittest.TestCase):
    """Una soglia misurata sul campo non si abbassa perché un ticket lo chiede.

    Il controllo non difende il numero: difende la RAGIONE, che vive nel
    commento accanto. Chi lo cambia deve rimuovere anche questo test, e in quel
    momento legge perché era lì — che è tutta la differenza fra una decisione e
    una svista.
    """

    def test_il_silenzio_ammesso_resta_tre_minuti(self) -> None:
        self.assertEqual(180, S.WATCHDOG_SILENCE)

    def test_il_perche_e_scritto_accanto_al_numero(self) -> None:
        import inspect
        sorgente = inspect.getsource(S)
        testa = sorgente[:sorgente.index("WATCHDOG_TICK")]
        self.assertIn("#358", testa)


class LoStrumentoEAccesoTests(unittest.TestCase):
    """Uno strumento che nessuno accende non esiste.

    È la stessa forma di difetto della dipendenza dichiarata e mai montata: il
    modulo c'è, i test sono verdi, e in produzione non misura niente. Qui pesa
    il doppio perché il guasto che deve cogliere è raro — ce ne accorgeremmo la
    notte in cui serviva, cioè troppo tardi per la volta dopo.
    """

    def test_il_battito_parte_col_processo(self) -> None:
        import inspect
        from .. import main
        self.assertIn("loop_lag.heartbeat", inspect.getsource(main._lifespan))
