"""clodia-platform#492, mitigazione (b) · un turno ucciso prima di produrre qualcosa si rimanda, una volta.

Il 2 ott 2026 quattro turni sono morti con `exit -9` fra le 18:50 e le 18:57,
uno dei quali su richiesta di un owner che aspettava una bozza. Il subprocess
era stato ucciso da fuori: il messaggio non era stato elaborato, la sessione era
già stata ricreata dal self-heal — e nessuno l'ha rimandato. Chi aveva scritto
ha visto un banner d'errore e basta.

**Perché rimandare è sicuro solo a zero eventi.** Se dall'SDK non è arrivato
nessun evento, il modello non ha emesso niente: nessuna tool call eseguita,
nessuna bolla pubblicata, nessun file toccato. Con anche un solo evento quella
garanzia cade e il rimando diventerebbe una seconda esecuzione di effetti
collaterali già avvenuti — un bonifico proposto due volte è peggio di un turno
perso. Il conteggio lo porta l'eccezione (`test_492_turno_ucciso`); qui si
misura che venga usato come condizione, e non come suggerimento.

**Perché UNA sola volta.** Se la causa è di host (pressione di risorse, OOM,
reaper), riprovare in cerchio moltiplica proprio il carico che ha ucciso il
turno. Un tentativo recupera l'incidente isolato; il secondo è una decisione che
va presa su una causa misurata, e la causa qui è dichiarata non determinabile
nella issue stessa.
"""
from __future__ import annotations

import ast
import inspect
import unittest

from . import channels
from ..sdk_runtime.session import SessioneTerminata


def _ucciso(*, ripristinata: bool = True, eventi: int = 0) -> SessioneTerminata:
    """La forma esatta con cui #492 arriva qui: processo ucciso, sessione
    ricreata, nessun evento SDK."""
    from claude_agent_sdk import ProcessError
    return SessioneTerminata(ProcessError("Command failed", exit_code=-9),
                             ripristinata, segnale=9, eventi=eventi)


class _Chat:
    """Una sessione finta che recita una lista di esiti, uno per invio."""

    def __init__(self, *esiti) -> None:
        self.esiti = list(esiti)
        self.inviati: list[str] = []

    async def send_user_message(self, prompt: str) -> str:
        self.inviati.append(prompt)
        esito = self.esiti.pop(0)
        if isinstance(esito, BaseException):
            raise esito
        return esito


class _Invio(unittest.IsolatedAsyncioTestCase):

    async def _invia(self, chat: _Chat):
        return await channels._invia_con_un_ritentativo(
            chat, "fammi la bozza", tier="SEAL-1", name="hedge-iot-new",
            responder="clodia-400")


class UnTurnoMortoSenzaAverProdottoNienteSiRimandaTests(_Invio):

    async def test_il_secondo_tentativo_risponde_e_la_stanza_non_resta_muta(self) -> None:
        """Rosso prima: l'eccezione usciva al primo colpo e il turno era perso."""
        chat = _Chat(_ucciso(), "ecco la bozza")
        self.assertEqual("ecco la bozza", await self._invia(chat))
        self.assertEqual(2, len(chat.inviati))
        self.assertEqual(["fammi la bozza", "fammi la bozza"], chat.inviati)

    async def test_il_turno_normale_non_paga_niente(self) -> None:
        chat = _Chat("risposta")
        self.assertEqual("risposta", await self._invia(chat))
        self.assertEqual(1, len(chat.inviati))


class QuandoNonSiRimandaTests(_Invio):
    """I tre casi in cui il rimando sarebbe sbagliato, non solo inutile."""

    async def test_se_il_modello_aveva_gia_parlato_non_si_rimanda(self) -> None:
        """Un evento SDK ricevuto significa che il turno può aver già agito:
        rimandarlo raddoppierebbe gli effetti collaterali."""
        chat = _Chat(_ucciso(eventi=1), "non deve arrivarci")
        with self.assertRaises(SessioneTerminata):
            await self._invia(chat)
        self.assertEqual(1, len(chat.inviati))

    async def test_se_la_sessione_non_e_ripartita_non_si_rimanda(self) -> None:
        """Senza sessione viva il secondo invio fallisce uguale: sarebbe solo un
        errore in più nel log e un'attesa in più per chi guarda."""
        chat = _Chat(_ucciso(ripristinata=False), "non deve arrivarci")
        with self.assertRaises(SessioneTerminata):
            await self._invia(chat)
        self.assertEqual(1, len(chat.inviati))

    async def test_un_errore_qualunque_non_si_rimanda(self) -> None:
        """Il difetto che non si ripara da sé si ripresenta identico: riprovare
        nasconde il guasto dietro un ritardo."""
        boom = RuntimeError("client wedged")
        chat = _Chat(boom, "non deve arrivarci")
        with self.assertRaises(RuntimeError) as ctx:
            await self._invia(chat)
        self.assertIs(boom, ctx.exception)
        self.assertEqual(1, len(chat.inviati))


class SiRimandaUnaVoltaSolaTests(_Invio):

    async def test_due_morti_di_fila_non_fanno_un_terzo_tentativo(self) -> None:
        chat = _Chat(_ucciso(), _ucciso(), "mai raggiunta")
        with self.assertRaises(SessioneTerminata):
            await self._invia(chat)
        self.assertEqual(2, len(chat.inviati))

    async def test_chi_legge_il_canale_sa_che_era_gia_stato_ritentato(self) -> None:
        """Altrimenti il messaggio di guasto sembra il primo, e la prossima
        mossa suggerita a chi guarda («rimandalo») è già stata fatta."""
        chat = _Chat(_ucciso(), _ucciso())
        with self.assertRaises(SessioneTerminata) as ctx:
            await self._invia(chat)
        self.assertIn("già", channels._diagnosi(ctx.exception).lower())
        self.assertIn("ritentat", channels._diagnosi(ctx.exception).lower())

    async def test_senza_ritentativo_la_diagnosi_resta_quella_di_prima(self) -> None:
        """Non-regressione su #397: la nota dell'eccezione è ciò che si legge."""
        err = _ucciso()
        testo = channels._diagnosi(err)
        self.assertIn("non è stato elaborato", testo)
        self.assertNotIn("ritentat", testo.lower())


class IlRitentativoEIlSoloPuntoDiInvioTests(unittest.TestCase):
    """Guardia statica: il caso è una CLASSE di chiamate, non una chiamata.

    Un secondo `chat.send_user_message(...)` aggiunto domani in `channels.py`
    rinascerebbe senza rimando e senza che nessun test lo noti — il percorso del
    ticket resterebbe corretto e quello accanto no.
    """

    def test_nessun_invio_diretto_fuori_dal_ritentativo(self) -> None:
        albero = ast.parse(inspect.getsource(channels))
        fuori = [
            n.lineno for n in ast.walk(albero)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", "") == "send_user_message"
            and not _dentro(albero, n, "_invia_con_un_ritentativo")
        ]
        self.assertEqual([], fuori,
                         f"invio diretto alla sessione alle righe {fuori}: "
                         f"quel turno non verrà mai rimandato")


def _dentro(albero: ast.AST, nodo: ast.AST, funzione: str) -> bool:
    for n in ast.walk(albero):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == funzione:
            if any(d is nodo for d in ast.walk(n)):
                return True
    return False


if __name__ == "__main__":
    unittest.main()
