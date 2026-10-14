"""Un turno opencode SCADUTO senza aver prodotto niente si rimanda una volta
(clodia-platform#514).

Il retry della #492 riconosce `SessioneTerminata` e nient'altro: un
`OpenCodeTurnTimeout` — tre occorrenze in un'ora su `hedge-iot-new` — finiva
dritto nell'annuncio di guasto, e il messaggio dell'owner restava non elaborato
benché il runtime non avesse prodotto nulla da ripetere.

Delle tre condizioni della #492 qui ne vale una sola, ed è quella che conta:
**zero eventi di progresso**. Le altre due (processo morto, sessione ricreata)
non riguardano questo caso — `opencode serve` è vivo e la sessione è
utilizzabile: è la risposta a non essere arrivata entro il budget, e il turno
scaduto è già stato abortito lato opencode prima che l'eccezione salisse.
"""
from __future__ import annotations

import unittest

from . import channels
from ..sdk_runtime.session import OpenCodeTurnTimeout, SessioneTerminata
from .test_492_retry_turno_ucciso import _RetryHarness, _Chat


def _scaduto(eventi: "int | None" = 0) -> OpenCodeTurnTimeout:
    """L'eccezione esatta della #514, come la costruisce `_run_turn`."""
    return OpenCodeTurnTimeout(
        "turno opencode scaduto (originale): attesi 180s sul budget di 180s "
        "per il turno — modello glm-5.2 — turno interrotto", eventi=eventi)


class QualeTurnoSiRimandaTests(unittest.TestCase):
    """L'unità della decisione, dove è leggibile senza montare un turno."""

    def test_un_turno_scaduto_a_vuoto_si_rimanda(self) -> None:
        """IL DIFETTO, in forma di test: oggi `_turno_ritentabile` guarda solo
        `SessioneTerminata` e questo caso esce False."""
        self.assertTrue(channels._turno_ritentabile(_scaduto()))

    def test_un_turno_scaduto_che_aveva_gia_lavorato_non_si_rimanda(self) -> None:
        """Il vincolo che rende il rimando sicuro: dal primo evento in poi il
        turno può aver già spedito una mail o scritto un file."""
        self.assertFalse(channels._turno_ritentabile(_scaduto(eventi=3)))

    def test_non_sapere_quanti_eventi_vale_quanto_averne_avuti(self) -> None:
        """«Non so» non è «zero»: l'assenza della prova non è una prova."""
        self.assertFalse(channels._turno_ritentabile(_scaduto(eventi=None)))
        self.assertFalse(channels._turno_ritentabile(
            OpenCodeTurnTimeout("scaduto")))

    def test_la_regola_della_492_resta_intatta(self) -> None:
        """Il confine dall'altra parte: il turno UCCISO continua a pretendere
        anche la sessione ricreata, che per un timeout non ha senso chiedere."""
        self.assertFalse(channels._turno_ritentabile(
            SessioneTerminata(RuntimeError("x"), False, eventi=0)))
        self.assertTrue(channels._turno_ritentabile(
            SessioneTerminata(RuntimeError("x"), True, eventi=0)))


class IlTurnoScadutoRipartTests(_RetryHarness):
    """Lo stesso percorso della #492, con l'eccezione del timeout in ingresso."""

    async def test_il_turno_scaduto_a_vuoto_viene_ritentato(self) -> None:
        chat = _Chat([_scaduto()])
        esito = await self._run(chat)
        self.assertEqual(2, chat.chiamate, "il turno scaduto non è stato ritentato")
        self.assertEqual("fatto", esito)
        self.assertIn(("clodia", "fatto"), self.posts)
        self.assertEqual([], self.annunci, "ritentato con successo: niente da annunciare")

    async def test_con_eventi_alle_spalle_si_annuncia_e_basta(self) -> None:
        chat = _Chat([_scaduto(eventi=5)])
        await self._run(chat)
        self.assertEqual(1, chat.chiamate)
        self.assertEqual(1, len(self.annunci))
        self.assertFalse(self.annunci[0]["ritentato"])

    async def test_si_ritenta_una_volta_sola(self) -> None:
        """Due timeout di fila sono 2× il budget del turno: oltre, l'attesa di
        chi aspetta in canale conterebbe più della risposta."""
        chat = _Chat([_scaduto(), _scaduto()])
        await self._run(chat)
        self.assertEqual(2, chat.chiamate)
        self.assertEqual(1, len(self.annunci))
        self.assertTrue(self.annunci[0]["ritentato"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
