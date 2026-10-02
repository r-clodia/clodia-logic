"""clodia-platform#492 · un turno ucciso da un segnale lo dice, e si sa se ha prodotto qualcosa.

L'incidente (2 ott 2026, 18:57:55): il turno di `clodia-400` su
`SEAL-1/hedge-iot-new` è morto con

    ProcessError: Command failed with exit code -9

e nel canale non è comparsa nessuna diagnosi utile. `-9` non è un programma che
fallisce: è **SIGKILL**, cioè qualcuno fuori dall'agent-server che termina il
subprocess — OOM killer dell'host, un reaper, uno stop manuale. È l'unica cosa
che chi legge avrebbe dovuto trovare scritta, perché decide da che parte si
cerca il guasto la volta dopo (e qui ha mandato sysadmin a cercare un bug
dell'applicazione per due ore).

Perché il controllo esistente non l'ha visto: `_sessione_terminata` riconosce
`CLIConnectionError` e i testi di `_PROCESSO_MORTO_RE` («terminated process»,
«process exited», «closed pipe»). `ProcessError('Command failed with exit code
-9')` non è né l'una né gli altri, quindi il turno usciva **non rivestito**: nel
canale il `repr` grezzo, e nessuna delle due informazioni che servono — che il
messaggio non è stato elaborato e che la sessione è già di nuovo buona.

La seconda metà di questo file è il dato su cui si fonda il ritentativo
automatico (#492, mitigazione (b)): **quanti eventi SDK ha ricevuto il turno
morto**. Zero eventi = il modello non ha emesso niente, quindi nessuna tool call
e nessuna bolla: rimandare il messaggio non può duplicare nulla. Con anche un
solo evento quella garanzia cade, e il ritentativo diventa una seconda
esecuzione di effetti collaterali già avvenuti. Il consumatore sta in
`api/channels.py` (`test_492_ritenta_una_volta`); qui si misura che il numero
esista e sia giusto.

NON è in scope: chi manda il SIGKILL (serve `dmesg`/metriche host, dichiarato
non determinabile da sysadmin nei commenti della issue) e il logging del reaper,
che è di #478/PR #502.
"""
from __future__ import annotations

import unittest
from unittest import mock

from claude_agent_sdk import CLIConnectionError, ProcessError

from . import session as S
from ..core.models import ClodiaStatus
from .test_session_recovery import _make_session, _patch_seams

#: L'errore esatto dell'incidente: exit -9 nella forma che usa `subprocess`.
_UCCISO = ProcessError("Command failed", exit_code=-9)


class IlCodiceDiUscitaDiceSeEUnKillTests(unittest.TestCase):
    """Due convenzioni per lo stesso fatto, e vanno lette entrambe.

    `subprocess` espone il segnale come `-N`; una shell lo espone come `128+N`.
    Leggerne una sola lascia metà delle occorrenze senza diagnosi: `exit -9`
    (#492) e `exit 143` (#397) sono lo stesso evento visto da due parti.
    """

    def test_la_forma_negativa_di_subprocess(self) -> None:
        self.assertEqual(9, S._segnale_di_uscita(-9))
        self.assertEqual(15, S._segnale_di_uscita(-15))

    def test_la_forma_128_piu_n_della_shell(self) -> None:
        self.assertEqual(9, S._segnale_di_uscita(137))
        self.assertEqual(15, S._segnale_di_uscita(143))

    def test_un_fallimento_ordinario_non_e_un_kill(self) -> None:
        """Il falso positivo costoso: chiamare «ucciso» un `exit 1` manderebbe
        la diagnosi successiva a cercare un killer che non esiste."""
        for codice in (0, 1, 2, 127, 128, 160, None, "9", True):
            with self.subTest(codice=codice):
                self.assertIsNone(S._segnale_di_uscita(codice))


class _Caduta(unittest.IsolatedAsyncioTestCase):
    """Un turno che muore su `query()`; ritorna l'eccezione uscita."""

    async def _turno(self, errore: BaseException, ripristinata: bool = True):
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


class UnTurnoUccisoLoDiceTests(_Caduta):

    async def test_exit_meno_nove_e_riconosciuto_come_morte_del_processo(self) -> None:
        """Rosso prima: usciva `ProcessError` nuda, senza `nota_utente` — cioè
        senza «il messaggio non è stato elaborato» e senza «è stata ricreata»."""
        _sess, err = await self._turno(_UCCISO)
        nota = getattr(err, "nota_utente", "")
        self.assertTrue(nota, "l'eccezione non porta nessuna nota leggibile")
        self.assertIn("non è stato elaborato", nota)
        self.assertIn("ricreata", nota)

    async def test_la_nota_nomina_il_segnale_e_scagiona_modello_e_provider(self) -> None:
        """La riga che mancava al canale il 2 ott: non è il modello, non è il
        provider — è un kill arrivato da fuori."""
        _sess, err = await self._turno(_UCCISO)
        nota = getattr(err, "nota_utente", "")
        self.assertIn("SIGKILL", nota)
        self.assertIn("ucciso", nota.lower())
        self.assertIn("modello", nota.lower())

    async def test_il_segnale_resta_leggibile_a_macchina(self) -> None:
        _sess, err = await self._turno(_UCCISO)
        self.assertEqual(9, getattr(err, "segnale", None))

    async def test_il_dettaglio_tecnico_non_si_butta(self) -> None:
        """Regola già fissata da `test_turn_failure_announce`: l'errore si dice,
        non si riassume."""
        _sess, err = await self._turno(_UCCISO)
        nota = getattr(err, "nota_utente", "")
        self.assertIn("ProcessError", nota)
        self.assertIn("-9", nota)

    async def test_anche_la_forma_143_della_shell(self) -> None:
        """#397 arrivava come `CLIConnectionError` e veniva già rivestita; ora
        porta ANCHE il segnale, che è l'informazione che le mancava."""
        _sess, err = await self._turno(
            CLIConnectionError("Cannot write to terminated process (exit code: 143)"))
        self.assertEqual(15, getattr(err, "segnale", None))
        self.assertIn("SIGTERM", getattr(err, "nota_utente", ""))

    async def test_un_fallimento_con_exit_uno_non_viene_rivestito(self) -> None:
        """Il guasto che non si ripara da sé non deve ricevere la promessa
        «basta rimandarlo»: sarebbe falsa."""
        boom = ProcessError("Command failed", exit_code=1)
        _sess, err = await self._turno(boom)
        self.assertIs(boom, err)
        self.assertEqual("", getattr(err, "nota_utente", ""))


class QuantiEventiAvevaRicevutoIlTurnoTests(_Caduta):
    """Il dato che rende sicuro (o vietato) il ritentativo automatico."""

    async def test_un_turno_morto_sull_invio_non_ha_ricevuto_niente(self) -> None:
        """Rosso prima: l'attributo non esisteva, quindi il chiamante non aveva
        modo di distinguere «non ha fatto niente» da «aveva già agito»."""
        _sess, err = await self._turno(_UCCISO)
        self.assertEqual(0, getattr(err, "eventi", None))

    async def test_gli_eventi_gia_arrivati_arrivano_fino_all_eccezione(self) -> None:
        """Se il subprocess muore DOPO che il modello ha parlato, il conteggio
        deve dirlo: è ciò che vieta il rimando."""
        sess = _make_session()
        sess._client = mock.AsyncMock()

        async def fake_recover():
            sess.status = ClodiaStatus.IDLE
            return True

        async def collect_poi_muori():
            # Come il ciclo vero: ogni messaggio dell'SDK passa da qui.
            sess._segna_evento_sdk()
            sess._segna_evento_sdk()
            raise _UCCISO

        with _patch_seams(), \
             mock.patch.object(sess, "_record", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_publish_error", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_refresh_provider_env", return_value=False), \
             mock.patch.object(sess, "_set_status", new=mock.AsyncMock(
                 side_effect=lambda s: setattr(sess, "status", s))), \
             mock.patch.object(sess, "_recover_session", side_effect=fake_recover), \
             mock.patch.object(sess, "_collect_response", side_effect=collect_poi_muori), \
             mock.patch.object(S.transcripts, "persist_for", lambda *_a: None), \
             mock.patch.object(S.activity_log, "append"):
            with self.assertRaises(Exception) as ctx:
                await sess.send_user_message("ciao")
        self.assertEqual(2, getattr(ctx.exception, "eventi", None))

    async def test_il_conteggio_riparte_a_ogni_turno(self) -> None:
        """La sessione è di lunga vita: un contatore che non si azzera
        vieterebbe per sempre il rimando dopo il primo turno riuscito."""
        sess, _err = await self._turno(_UCCISO)
        sess._eventi_turno = 7
        client = mock.AsyncMock()
        client.query.side_effect = _UCCISO
        sess._client = client

        async def fake_recover():
            sess.status = ClodiaStatus.IDLE
            return True

        with _patch_seams(), \
             mock.patch.object(sess, "_record", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_publish_error", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_refresh_provider_env", return_value=False), \
             mock.patch.object(sess, "_set_status", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_recover_session", side_effect=fake_recover), \
             mock.patch.object(S.activity_log, "append"):
            with self.assertRaises(Exception) as ctx:
                await sess.send_user_message("di nuovo")
        self.assertEqual(0, getattr(ctx.exception, "eventi", None))


class IlCicloEventiIncrementaDoveMisuraIlProgressoTests(unittest.TestCase):
    """Un solo punto per «è arrivato un evento SDK».

    Il contatore e `_last_event_at` sono la stessa osservazione: tenerli su due
    righe diverse è il modo di farne restare indietro una — lezione già pagata
    con `last_activity` (agents-notebook A13, `test_working_is_not_stuck`).
    """

    def test_il_collect_passa_dal_metodo_unico(self) -> None:
        import inspect
        src = inspect.getsource(S.ChatSession._collect_response)
        self.assertIn("_segna_evento_sdk()", src,
                      "il ciclo eventi non conta più gli eventi: il ritentativo "
                      "automatico perderebbe la sua unica garanzia")
        self.assertNotIn("self._last_event_at = ", src,
                         "due scritture della stessa verità: una resterà indietro")


if __name__ == "__main__":
    unittest.main()
