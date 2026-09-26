"""clodia-platform#397 (punto 3) · un turno caduto su una sessione morta lo dice.

L'incidente: il subprocess CLI di `clodia-290` è terminato da solo alle 16:33
(SIGTERM, exit 143); nessuno se n'è accorto fino al giro schedulato successivo,
alle 16:44, che ha provato a scriverci. La sessione è stata ricreata subito dopo
— il self-heal funziona — ma nel canale è comparso soltanto:

    ⚠️ Il turno di @clodia-290 è terminato con un errore [...]
    CLIConnectionError('Cannot write to terminated process (exit code: 143)')

Chi legge non ricava le due cose che servono: che il suo messaggio **non è stato
elaborato**, e che **non c'è niente da riparare** — la sessione è già di nuovo
buona, basta rimandarlo. È la stessa richiesta del punto 3 di #358, dove la cura
è stata far parlare l'eccezione invece di far indovinare chi la stampa: la
diagnosi esiste un frame più sotto e la buttava via un `raise` nudo.

Qui sotto (`_recover_session` è ciò che sa com'è finita) e non in
`_announce_failure`: l'annunciatore non può sapere se la sessione è stata
ricreata, e riconoscerlo dal testo dell'eccezione sarebbe indovinare due volte.

NON è in scope (punti 1-2 della issue, indagine runtime): la causa degli stalli
dell'event loop e un controllo proattivo di salute delle sessioni. Qui si
corregge solo ciò che la stanza legge.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from claude_agent_sdk import CLIConnectionError

from . import session as S
from ..core.models import ClodiaStatus
from .test_session_recovery import _make_session, _patch_seams

#: L'errore esatto dell'incidente.
_MORTO = CLIConnectionError("Cannot write to terminated process (exit code: 143)")


class _Caduta(unittest.IsolatedAsyncioTestCase):
    """Monta un turno che muore su `query()` e restituisce l'eccezione uscita."""

    async def _turno(self, errore: BaseException, ripristinata: bool):
        sess = _make_session()
        client = mock.AsyncMock()
        client.query.side_effect = errore
        sess._client = client

        async def fake_recover():
            sess.status = ClodiaStatus.IDLE if ripristinata else ClodiaStatus.ERROR
            return ripristinata

        with _patch_seams(), \
             mock.patch.object(sess, "_record", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_publish_error", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_refresh_provider_env", return_value=False), \
             mock.patch.object(sess, "_set_status", new=mock.AsyncMock(
                 side_effect=lambda s: setattr(sess, "status", s))), \
             mock.patch.object(sess, "_recover_session", side_effect=fake_recover), \
             mock.patch.object(S.activity_log, "append"):
            with self.assertRaises(Exception) as ctx:
                await sess.send_user_message("ciao")
        return sess, ctx.exception


class ADeadSessionSaysSoTests(_Caduta):

    async def test_the_note_tells_what_happened_and_what_to_do(self) -> None:
        """Rosso prima: usciva `CLIConnectionError(...)` e basta."""
        _sess, err = await self._turno(_MORTO, ripristinata=True)
        nota = getattr(err, "nota_utente", "")
        self.assertTrue(nota, "l'eccezione non porta nessuna nota leggibile")
        self.assertIn("non è stato elaborato", nota)
        self.assertIn("ricreata", nota)

    async def test_the_technical_detail_is_not_thrown_away(self) -> None:
        """L'errore va detto, non riassunto: è la regola già fissata in
        `test_turn_failure_announce`."""
        _sess, err = await self._turno(_MORTO, ripristinata=True)
        nota = getattr(err, "nota_utente", "")
        self.assertIn("exit code: 143", nota)
        self.assertIn("CLIConnectionError", nota)

    async def test_the_original_error_stays_reachable(self) -> None:
        _sess, err = await self._turno(_MORTO, ripristinata=True)
        self.assertIs(_MORTO, getattr(err, "causa", None))
        self.assertIs(_MORTO, err.__cause__)

    async def test_a_failed_recovery_does_not_promise_a_retry(self) -> None:
        """Se la sessione NON è ripartita, «riprova» sarebbe una bugia."""
        sess, err = await self._turno(_MORTO, ripristinata=False)
        nota = getattr(err, "nota_utente", "")
        self.assertIn("non è stato possibile ricrearla", nota.lower())
        self.assertNotIn("basta rimandarlo", nota)
        self.assertEqual(ClodiaStatus.ERROR, sess.status)

    async def test_a_process_death_without_that_exception_class_counts_too(self) -> None:
        """La classe non è l'unico segnale: altri runtime (codex, opencode)
        muoiono con la stessa sostanza e un'altra eccezione."""
        _sess, err = await self._turno(
            RuntimeError("write to closed pipe: process exited"), ripristinata=True)
        self.assertIn("non è stato elaborato", getattr(err, "nota_utente", ""))

    async def test_an_ordinary_failure_is_left_alone(self) -> None:
        """Un turno morto per altro non va rivestito: la nota direbbe una cosa
        falsa («basta rimandare») su un guasto che non si ripara da sé."""
        boom = RuntimeError("client wedged")
        _sess, err = await self._turno(boom, ripristinata=True)
        self.assertIs(boom, err)
        self.assertEqual("", getattr(err, "nota_utente", ""))

    async def test_the_lock_is_free_and_the_session_is_ready(self) -> None:
        """L'invariante di `test_session_recovery` non si rompe per strada."""
        sess, _err = await self._turno(_MORTO, ripristinata=True)
        self.assertFalse(sess._lock.locked())
        self.assertEqual(ClodiaStatus.IDLE, sess.status)


class TheCollectPhaseIsCoveredTooTests(unittest.IsolatedAsyncioTestCase):
    """Il subprocess può morire anche DOPO l'invio, mentre si raccoglie la
    risposta: stessa sostanza, stesso messaggio."""

    async def test_a_death_while_collecting_is_wrapped_as_well(self) -> None:
        sess = _make_session()
        sess._client = mock.AsyncMock()

        async def fake_recover():
            sess.status = ClodiaStatus.IDLE
            return True

        async def boom():
            raise _MORTO

        with _patch_seams(), \
             mock.patch.object(sess, "_record", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_publish_error", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_refresh_provider_env", return_value=False), \
             mock.patch.object(sess, "_set_status", new=mock.AsyncMock(
                 side_effect=lambda s: setattr(sess, "status", s))), \
             mock.patch.object(sess, "_recover_session", side_effect=fake_recover), \
             mock.patch.object(sess, "_collect_response", side_effect=boom), \
             mock.patch.object(S.transcripts, "persist_for", lambda *_a: None), \
             mock.patch.object(S.activity_log, "append"):
            with self.assertRaises(Exception) as ctx:
                await sess.send_user_message("ciao")
        self.assertIn("non è stato elaborato",
                      getattr(ctx.exception, "nota_utente", ""))


class TimeoutsAreNotSessionDeathsTests(unittest.IsolatedAsyncioTestCase):
    """Ciò che NON va riscritto.

    L'anello finale — la nota che arriva fino al messaggio del canale — si
    misura in `server/api/test_turn_failure_announce.py`, dove vive
    `_announce_failure`.
    """

    async def test_asyncio_timeout_stays_a_timeout(self) -> None:
        """Guardia di non-regressione su #358: un timeout non è una sessione
        morta e non deve essere riscritto."""
        sess = _make_session()
        client = mock.AsyncMock()
        client.query.side_effect = asyncio.TimeoutError()
        sess._client = client

        with _patch_seams(), \
             mock.patch.object(sess, "_record", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_publish_error", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_refresh_provider_env", return_value=False), \
             mock.patch.object(sess, "_set_status", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_recover_session",
                               new=mock.AsyncMock(return_value=True)), \
             mock.patch.object(S.activity_log, "append"):
            with self.assertRaises(asyncio.TimeoutError):
                await sess.send_user_message("ciao")


if __name__ == "__main__":
    unittest.main()
