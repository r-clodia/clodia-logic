"""clodia-platform#514 · il budget del turno è dell'AGENTE, e un turno scaduto a
vuoto si può rimandare.

L'incidente: su `hedge-iot-new` i turni di officer/officer-1 (opencode, glm-5.2
su Scaleway) muoiono tre volte in un'ora con `OpenCodeTurnTimeout ... 180s`.
Nessuna risposta in canale, e l'owner vede solo un errore di sistema.

Due difetti distinti, uno per sezione di questo file.

**Il budget era dell'istanza, non dell'agente.** `_OPENCODE_TURN_TIMEOUT` è una
variabile d'ambiente sola per tutti gli agenti: per dare più tempo all'esecutore
di tool che non converge bisognava darlo anche a chi, sullo stesso processo,
deve fallire in fretta — cioè togliere il fail-fast proprio dove funziona. La
soglia giusta dipende da modello, provider e tipo di lavoro, che sono proprietà
del seed; ora `AgentSpec.turn_timeout` la dichiara lì e il default di
piattaforma resta per chi tace.

**Il turno scaduto non veniva mai rimandato.** Il retry della #492 copre solo
`SessioneTerminata`: un timeout opencode non ci passa, quindi un turno morto
senza aver prodotto NIENTE veniva annunciato come guasto invece di essere
ripetuto. Il vincolo che rende il rimando sicuro è lo stesso della #492 — zero
eventi di progresso: dal primo in poi il turno può aver già chiamato dei tool, e
ripeterlo rifarebbe quegli effetti.

Limite noto e dichiarato: il conto degli eventi viene dallo stream SSE di
`opencode serve`. Se quel runtime emettesse un evento non-`server.*` già alla
PRESA IN CARICO del messaggio — prima di qualunque lavoro del modello — il conto
non sarebbe più zero e il rimando non scatterebbe. È il verso prudente
dell'errore (si perde un retry, non si duplica una mail), ma va misurato sul
campo e non dedotto da qui.

NON è in scope (deciso sulla issue): il fallback automatico su un altro modello
dopo il timeout, e i picchi di latenza del gateway `clodia-tools:7849` — quelli
vivono nell'altro componente e spiegano una sola delle tre occorrenze.
"""
from __future__ import annotations

import asyncio
import pathlib
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from . import session as S
from .test_423_budget_turno import APPESO, _Serve, _sessione


def _seed(turn_timeout):
    """Il seed dell'agente come lo vede `_kind_spec`, col solo campo che conta."""
    return SimpleNamespace(turn_timeout=turn_timeout)


class _TurnoBase(unittest.IsolatedAsyncioTestCase):
    """Un `_run_turn` vero contro l'`opencode serve` finto della #423.

    Il server è vero e non un mock di httpx per la ragione già scritta lì: la
    cosa da misurare è *quanto si aspetta*, e con httpx finto il tempo non
    esiste.
    """

    async def _turno(self, script, *, seed=None, default=30.0, durante=None):
        """Ritorna (errore, secondi, sessione). `durante` è una coroutine-factory
        eseguita mentre il turno è in corso (serve a far arrivare eventi)."""
        srv = _Serve(script)
        await srv.start()
        self.addAsyncCleanup(srv.stop)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sess = _sessione(srv.port)
        t0 = asyncio.get_running_loop().time()
        with mock.patch.object(S, "_OPENCODE_TURN_TIMEOUT", default), \
             mock.patch.object(S, "_OPENCODE_MIN_RETRY_BUDGET", 0.5), \
             mock.patch.object(S, "_kind_spec", return_value=seed), \
             mock.patch.object(S, "_resolve_sessions_dir",
                               return_value=pathlib.Path(d.name)), \
             mock.patch.object(S.activity_log, "append"):
            task = asyncio.ensure_future(sess._run_turn("riconcilia il foglio"))
            if durante is not None:
                await asyncio.sleep(0.2)
                durante(sess)
            with self.assertRaises(RuntimeError) as ctx:
                await task
        return ctx.exception, asyncio.get_running_loop().time() - t0, sess


class IlBudgetVieneDalSeedTests(_TurnoBase):
    """§1 — `turn_timeout` nel seed vince sul default di piattaforma."""

    async def test_il_seed_accorcia_il_turno(self) -> None:
        """Rosso prima: il turno dura il default (30s) e il messaggio annuncia
        un budget che non è quello dell'agente."""
        err, secondi, _s = await self._turno([APPESO], seed=_seed(1), default=30.0)
        self.assertLess(secondi, 3.0,
                        f"il budget del seed (1s) non è stato usato: {secondi:.1f}s")
        self.assertIn("budget di 1s", str(err),
                      f"il messaggio non dichiara il budget dell'agente: {err}")

    async def test_senza_dichiarazione_resta_il_default_di_piattaforma(self) -> None:
        """Il ramo che già funzionava: se passasse solo il test sopra, il verde
        sarebbe per il motivo sbagliato."""
        err, secondi, _s = await self._turno([APPESO], seed=_seed(None), default=1.0)
        self.assertLess(secondi, 3.0, f"{secondi:.1f}s")
        self.assertIn("budget di 1s", str(err))

    def test_un_valore_inutile_non_azzera_il_turno(self) -> None:
        """Un budget nullo o illeggibile fermerebbe OGNI turno dell'agente: la
        lettura ricade sul default invece di propagare il guasto."""
        sess = S.OpenCodeChatSession.__new__(S.OpenCodeChatSession)
        sess.kind = "officer"
        with mock.patch.object(S, "_OPENCODE_TURN_TIMEOUT", 180.0):
            for valore in (None, 0, "", "x", _seed):
                with mock.patch.object(S, "_kind_spec", return_value=_seed(valore)):
                    self.assertEqual(180.0, sess._turn_timeout_s(), f"valore={valore!r}")
            with mock.patch.object(S, "_kind_spec", return_value=None):
                self.assertEqual(180.0, sess._turn_timeout_s(), "registry assente")
            with mock.patch.object(S, "_kind_spec", return_value=_seed(600)):
                self.assertEqual(600.0, sess._turn_timeout_s())

    def test_il_valore_si_rilegge_a_ogni_turno(self) -> None:
        """Una PATCH sul seed deve valere dal turno successivo, non dal prossimo
        riavvio: il budget non si memorizza sulla sessione."""
        sess = S.OpenCodeChatSession.__new__(S.OpenCodeChatSession)
        sess.kind = "officer"
        with mock.patch.object(S, "_kind_spec", return_value=_seed(60)):
            self.assertEqual(60.0, sess._turn_timeout_s())
        with mock.patch.object(S, "_kind_spec", return_value=_seed(300)):
            self.assertEqual(300.0, sess._turn_timeout_s())


class IlTurnoScadutoPortaIlContoDegliEventiTests(_TurnoBase):
    """§2 — l'eccezione dice se quel turno aveva prodotto qualcosa."""

    async def test_un_turno_appeso_senza_eventi_dichiara_zero(self) -> None:
        """Rosso prima: `OpenCodeTurnTimeout` non portava nessun conto, quindi
        `_turno_ritentabile` non poteva distinguere questo caso da un turno che
        aveva già chiamato dei tool."""
        err, _s, _sess = await self._turno([APPESO], seed=_seed(1))
        self.assertIsInstance(err, S.OpenCodeTurnTimeout)
        self.assertEqual(0, err.eventi)

    async def test_un_turno_che_aveva_gia_prodotto_lo_dichiara(self) -> None:
        """La riga SSE arriva mentre il turno è appeso: da lì in poi il rimando
        non è più innocuo."""
        def evento(sess):
            sess._note_event_line('data: {"type":"message.part.updated"}')

        err, _s, _sess = await self._turno([APPESO], seed=_seed(1), durante=evento)
        self.assertEqual(1, err.eventi)

    async def test_il_conto_riparte_a_ogni_turno(self) -> None:
        """Il conto del turno precedente, lasciato lì, direbbe «aveva già
        lavorato» su un turno che non ha fatto niente — e toglierebbe il rimando
        proprio nel caso in cui è sicuro."""
        srv = _Serve([APPESO])
        await srv.start()
        self.addAsyncCleanup(srv.stop)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sess = _sessione(srv.port)
        sess._eventi_turno = 7              # il turno di prima aveva lavorato
        with mock.patch.object(S, "_OPENCODE_TURN_TIMEOUT", 1.0), \
             mock.patch.object(S, "_kind_spec", return_value=None), \
             mock.patch.object(S, "_resolve_sessions_dir",
                               return_value=pathlib.Path(d.name)), \
             mock.patch.object(S.activity_log, "append"):
            with self.assertRaises(S.OpenCodeTurnTimeout) as ctx:
                await sess._run_turn("riconcilia il foglio")
        self.assertEqual(0, ctx.exception.eventi)

    async def test_il_default_delleccezione_e_non_so(self) -> None:
        """Chi costruisce l'eccezione senza il conto non ottiene il rimando per
        difetto: l'assenza della prova non è una prova (stessa regola di
        `SessioneTerminata`)."""
        self.assertIsNone(S.OpenCodeTurnTimeout("scaduto").eventi)


class SoloIlProgressoContaTests(unittest.TestCase):
    """`server.heartbeat` arriva ogni ~10s a sessione FERMA: contarlo renderebbe
    ogni turno «uno che aveva già lavorato», cioè mai ritentabile."""

    def setUp(self) -> None:
        self.sess = S.OpenCodeChatSession.__new__(S.OpenCodeChatSession)
        self.sess._eventi_turno = 0

    def test_una_riga_di_progresso_conta(self) -> None:
        self.assertTrue(self.sess._note_event_line('data: {"type":"message.updated"}'))
        self.assertEqual(1, self.sess._eventi_turno)

    def test_gli_eventi_del_server_non_contano(self) -> None:
        for riga in ('data: {"type":"server.heartbeat"}',
                     'data: {"type":"server.connected"}',
                     'data: {"type":""}',
                     'event: ping',
                     'data: non-json'):
            self.assertFalse(self.sess._note_event_line(riga), riga)
        self.assertEqual(0, self.sess._eventi_turno)

    def test_una_sessione_senza_attributo_non_esplode(self) -> None:
        """Un contatore diagnostico non deve poter uccidere lo stream che lo
        alimenta: le sessioni costruite senza `__init__` non hanno l'attributo."""
        nuda = S.OpenCodeChatSession.__new__(S.OpenCodeChatSession)
        self.assertTrue(nuda._note_event_line('data: {"type":"message.updated"}'))
        self.assertEqual(1, nuda._eventi_turno)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
