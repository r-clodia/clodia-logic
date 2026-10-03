"""Un turno ucciso dall'esterno si legge come ucciso (clodia-platform#492).

L'incidente: il turno di clodia-400 su `SEAL-1/hedge-iot-new` muore con

    ProcessError: Command failed with exit code -9

e nel canale compare quel `repr` e basta. Exit -9 è POSIX per «il subprocess è
stato terminato dal segnale 9», cioè SIGKILL: qualcuno di esterno — l'OOM killer
dell'host, il reaper, un riavvio — l'ha ucciso a metà. Chi legge il canale vede
una stringa indistinguibile da un errore del modello o del provider, e la prima
ipotesi non è mai «è stato ucciso».

Perché arrivava nuda: `_sessione_terminata` riconosceva un processo morto in due
modi soli — la classe `CLIConnectionError`, oppure una delle frasi di
`_PROCESSO_MORTO_RE`. Una `ProcessError` con `exit_code=-9` non è né l'una né
l'altra. L'exit 143 del reaper (#478) si salvava per CASO, perché il runtime lo
riveste in «Cannot write to terminated process (exit code: 143)»: era la frase,
non il codice, a essere riconosciuta — quindi la stessa morte raccontata in
un'altra forma ricadeva nel silenzio.

Qui si verifica il riconoscimento e ciò che l'eccezione porta con sé: il segnale
(per il testo distinto) e il conto degli eventi SDK del turno caduto, che è il
dato su cui `api/channels._turno_ritentabile` decide se rimandarlo.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from claude_agent_sdk import CLIConnectionError
from claude_agent_sdk._errors import ProcessError

from . import session as S
from ..core.models import ClodiaStatus
from .test_session_recovery import _make_session, _patch_seams


class KillSignalIsRecognisedTests(unittest.TestCase):
    def test_exit_negativo_e_128_piu_n_sono_lo_stesso_segnale(self) -> None:
        """Le due convenzioni con cui la stessa morte viene riportata."""
        self.assertEqual(9, S._segnale_di_kill(ProcessError("x", exit_code=-9)))
        self.assertEqual(9, S._segnale_di_kill(ProcessError("x", exit_code=137)))
        self.assertEqual(15, S._segnale_di_kill(ProcessError("x", exit_code=-15)))
        self.assertEqual(15, S._segnale_di_kill(ProcessError("x", exit_code=143)))

    def test_un_uscita_qualunque_non_e_un_kill(self) -> None:
        """Il confine: exit 1 è «il comando è andato male», non «qualcuno l'ha
        ucciso». Chiamarlo kill rivestirebbe di una promessa («basta
        rimandarlo») un guasto che si ripresenterebbe identico."""
        self.assertIsNone(S._segnale_di_kill(ProcessError("x", exit_code=1)))
        self.assertIsNone(S._segnale_di_kill(RuntimeError("niente codici qui")))

    def test_il_codice_si_legge_anche_dal_testo(self) -> None:
        """Quando un runtime riveste l'errore di un altro, l'attributo
        strutturato si perde e resta solo la frase: è il caso dell'exit 143 del
        reaper, che oggi si salva per questa strada."""
        self.assertEqual(
            15, S._segnale_di_kill(
                RuntimeError("Cannot write to terminated process (exit code: 143)")))


class TheKilledTurnSaysSoTests(unittest.TestCase):
    def test_il_sigkill_diventa_sessione_terminata(self) -> None:
        """IL DIFETTO, in forma di test: oggi `_sessione_terminata` torna None
        per la `ProcessError` della #492, quindi `_announce_failure` pubblica il
        `repr` nudo."""
        err = ProcessError("Command failed with exit code -9", exit_code=-9)
        parlante = S._sessione_terminata(err, True)
        self.assertIsNotNone(parlante, "exit -9 non riconosciuto: è il difetto di #492")
        self.assertEqual(9, parlante.segnale)
        self.assertIs(err, parlante.causa)

    def test_il_testo_e_diverso_da_quello_di_una_sessione_caduta(self) -> None:
        """La distinzione nasce dal fatto che sono due testi diversi: uno dice
        «la sessione era terminata», l'altro dice CHI l'ha terminata e che non è
        stato il modello. Se fossero uguali, riconoscere il segnale non
        servirebbe a nessuno."""
        ucciso = S._sessione_terminata(ProcessError("x", exit_code=-9), True)
        caduto = S._sessione_terminata(CLIConnectionError("broken pipe"), True)
        self.assertNotEqual(caduto.nota_utente, ucciso.nota_utente)
        self.assertIn("SIGKILL", ucciso.nota_utente)
        self.assertIn("terminato dall'esterno", ucciso.nota_utente)
        self.assertIsNone(caduto.segnale)
        self.assertNotIn("SIGKILL", caduto.nota_utente)

    def test_il_blocco_del_loop_entra_nella_nota(self) -> None:
        """La metà (c) della #473: la correlazione si legge dov'è il guasto, non
        si deduce confrontando i timestamp di due log."""
        parlante = S._sessione_terminata(
            ProcessError("x", exit_code=-9), True,
            blocco_loop="l'event loop si era bloccato 5739s, finito 3100s prima")
        self.assertIn("5739s", parlante.nota_utente)

    def test_gli_eventi_del_turno_morto_viaggiano_con_l_eccezione(self) -> None:
        """Senza questo numero il canale non può decidere se ritentare: è
        l'unica prova che il turno non aveva ancora eseguito nessun tool."""
        self.assertEqual(
            0, S._sessione_terminata(ProcessError("x", exit_code=-9), True,
                                     eventi=0).eventi)
        self.assertEqual(
            7, S._sessione_terminata(ProcessError("x", exit_code=-9), True,
                                     eventi=7).eventi)

    def test_chi_non_sa_quanti_eventi_dice_non_so_e_non_zero(self) -> None:
        """Il default è `None`, non `0`. Zero significa «non ha fatto niente,
        rimandalo pure»: dirlo per difetto regalerebbe il rimando proprio nel
        caso in cui manca la prova che lo rende innocuo. Oggi l'unico
        costruttore passa sempre il valore — è una trappola per il prossimo,
        non un difetto attivo, e si chiude adesso che costa una riga."""
        self.assertIsNone(
            S._sessione_terminata(ProcessError("x", exit_code=-9), True).eventi)
        self.assertIsNone(S.SessioneTerminata(RuntimeError("x"), True).eventi)

    def test_una_cancellazione_resta_una_cancellazione(self) -> None:
        """Guardia preesistente, qui perché il riconoscimento nuovo non deve
        allargarla: un turno interrotto dall'utente non è un turno ucciso."""
        self.assertIsNone(S._sessione_terminata(asyncio.CancelledError(), True))


class TheEventCounterIsPerTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_ogni_evento_sdk_conta(self) -> None:
        sess = _make_session()

        class _Due:
            def receive_response(self):
                async def _gen():
                    yield object()
                    yield object()
                return _gen()

        sess._client = _Due()
        await sess._collect_response()
        self.assertEqual(2, sess._eventi_turno)

    async def test_il_contatore_non_uccide_il_turno_su_cui_gira(self) -> None:
        """Una sessione costruita senza `__init__` non ha l'attributo: con
        `+= 1` il turno morirebbe di `AttributeError` nel cuore della raccolta
        della risposta. Un contatore diagnostico non può costare il turno che
        sta misurando — e il fake di `_make_session` è esattamente una sessione
        così, come lo sarà il prossimo modo di istanziarla."""
        sess = _make_session()
        self.assertFalse(hasattr(sess, "_eventi_turno"),
                         "il fake ha l'attributo: il test non misura più niente")

        class _Uno:
            def receive_response(self):
                async def _gen():
                    yield object()
                return _gen()

        sess._client = _Uno()
        await sess._collect_response()
        self.assertEqual(1, sess._eventi_turno)

    async def test_il_conto_riparte_a_ogni_turno(self) -> None:
        """Se restasse quello del turno precedente, un turno ucciso prima di
        qualunque evento risulterebbe «aveva già lavorato» e non verrebbe mai
        ritentato: il difetto si curerebbe solo la prima volta."""
        sess = _make_session()
        sess._eventi_turno = 12
        client = mock.AsyncMock()
        client.query.side_effect = ProcessError("Command failed", exit_code=-9)
        sess._client = client

        with _patch_seams(), \
             mock.patch.object(sess, "_record", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_publish_error", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_refresh_provider_env", return_value=False), \
             mock.patch.object(sess, "_refresh_mcp_principal", return_value=False), \
             mock.patch.object(sess, "_set_status", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_recover_session",
                               new=mock.AsyncMock(return_value=True)), \
             mock.patch.object(S.activity_log, "append"):
            with self.assertRaises(S.SessioneTerminata) as ctx:
                await sess.send_user_message("ciao")

        self.assertEqual(9, ctx.exception.segnale)
        self.assertEqual(0, ctx.exception.eventi)
        self.assertTrue(ctx.exception.ripristinata)
        self.assertEqual(ClodiaStatus.IDLE, sess.status)
