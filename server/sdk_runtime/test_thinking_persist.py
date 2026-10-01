"""Il ragionamento di un turno sopravvive al turno (clodia-platform#484).

`thinking_chunk` era SOLO un evento sul bus: chi non era connesso mentre il
turno girava non aveva più niente da riaprire. Qui si guarda la cucitura in cui
il pezzo di ragionamento diventa insieme evento live e testo da conservare —
`_pubblica_pensiero` — e la consegna di quel testo a fine turno.

Il deposito su disco e il suo tetto stanno in `agents/test_reasoning_log.py`:
qui si verifica solo che l'accumulo esista, sia per turno, e si svuoti.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from . import session as S


class _Finta:
    """La parte di una sessione che il seam tocca: nient'altro serve."""

    def __init__(self) -> None:
        self.chat_id = "chan:SEAL-1:software-house:clodia"


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class IlSeamPubblicaEAccumula(unittest.TestCase):
    def setUp(self) -> None:
        self.pubblicati: list = []
        p = patch.object(S.bus, "publish", new=AsyncMock(
            side_effect=lambda ev: self.pubblicati.append(ev)))
        self.pub = p.start()
        self.addCleanup(p.stop)

    def test_il_chunk_esce_sul_bus_come_prima(self) -> None:
        s = _Finta()
        _run(S._pubblica_pensiero(s, 0, "sto ragionando"))
        self.assertEqual(["thinking_chunk"], [e.type for e in self.pubblicati])
        self.assertEqual("sto ragionando", self.pubblicati[0].payload["delta"])
        self.assertEqual(s.chat_id, self.pubblicati[0].payload["chat_id"])

    def test_lo_stesso_chunk_resta_disponibile_a_turno_finito(self) -> None:
        """IL DIFETTO SEGNALATO: finito lo streaming non restava niente."""
        s = _Finta()
        _run(S._pubblica_pensiero(s, 0, "primo passo"))
        _run(S._pubblica_pensiero(s, 1, "secondo passo"))
        raccolto = S.consuma_pensiero(s)
        self.assertIn("primo passo", raccolto["text"])
        self.assertIn("secondo passo", raccolto["text"])

    def test_il_testo_conservato_e_quello_cucito(self) -> None:
        """Non due copie con due formattazioni: lo storico è ciò che si è
        visto scorrere, separatori fra blocchi compresi (vedi `_ThinkSeam`)."""
        s = _Finta()
        _run(S._pubblica_pensiero(s, 0, "fine blocco."))
        _run(S._pubblica_pensiero(s, 1, "blocco nuovo."))
        live = "".join(e.payload["delta"] for e in self.pubblicati)
        self.assertEqual(live, S.consuma_pensiero(s)["text"])
        self.assertIn("\n\n", live)

    def test_un_delta_vuoto_non_pubblica_niente(self) -> None:
        s = _Finta()
        _run(S._pubblica_pensiero(s, 0, ""))
        self.assertEqual([], self.pubblicati)
        self.assertIsNone(S.consuma_pensiero(s))

    def test_consumare_svuota(self) -> None:
        """Il turno dopo non deve ereditare il ragionamento di questo: sarebbe
        il ragionamento giusto appeso alla bolla sbagliata."""
        s = _Finta()
        _run(S._pubblica_pensiero(s, 0, "del turno di prima"))
        self.assertIsNotNone(S.consuma_pensiero(s))
        self.assertIsNone(S.consuma_pensiero(s))

    def test_il_tetto_vale_gia_in_memoria(self) -> None:
        """Il buffer non cresce senza limite in attesa di essere capato su
        disco: un turno impazzito terrebbe centinaia di MB nel processo."""
        s = _Finta()
        for i in range(400):
            _run(S._pubblica_pensiero(s, i, "x" * 1000))
        raccolto = S.consuma_pensiero(s)
        self.assertTrue(raccolto["truncated"])
        self.assertLess(len(raccolto["text"]), 200_000)

    def test_senza_ragionamento_non_si_consuma_niente(self) -> None:
        self.assertIsNone(S.consuma_pensiero(_Finta()))


class IPuntiDiEmissionePassanoDiLi(unittest.TestCase):
    """Una cucitura che un runtime scavalca è un ragionamento perso a metà.

    I punti sono quattro (SDK Claude, codex, opencode `reasoning`, opencode
    `text` dirottato) e si sono già dimenticati una volta del `_ThinkSeam`
    appena nato: lo stesso errore qui significherebbe un turno il cui
    ragionamento c'è stato e non si ritrova.
    """

    def _sorgente(self) -> str:
        from pathlib import Path
        return (Path(__file__).parent / "session.py").read_text(encoding="utf-8")

    def test_tutti_e_quattro_chiamano_la_cucitura(self) -> None:
        src = self._sorgente()
        self.assertEqual(4, src.count("await _pubblica_pensiero("),
                         "i punti di emissione del ragionamento sono quattro")

    def test_nessuno_pubblica_un_thinking_chunk_per_conto_suo(self) -> None:
        src = self._sorgente()
        self.assertEqual(
            1, src.count('type="thinking_chunk"'),
            "thinking_chunk si pubblica SOLO dentro _pubblica_pensiero: un "
            "secondo punto sarebbe un ragionamento che non viene conservato")

    def test_ogni_runtime_apre_il_proprio_accumulo_a_inizio_turno(self) -> None:
        """Tre classi di sessione, tre `send_user_message`: se una non azzera,
        il suo ragionamento si accumula per tutta la vita della sessione."""
        src = self._sorgente()
        self.assertEqual(3, src.count("_inizia_pensiero(self)"),
                         "le classi di sessione sono tre")


if __name__ == "__main__":
    unittest.main()
