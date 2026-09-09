"""Un modello senza canale `reasoning` separato non deve poter scrivere il
proprio pensiero in chat.

Segnalato da Davide il 9 set 2026: il segretario (opencode, gemma-4-26b-a4b-it
su Scaleway) postava in canale l'intero ragionamento — fino a includere un
frammento della regola 5 di `platform-core.md` («igiene dell'output — solo la
risposta, mai il ragionamento»). Il provider non separa il pensiero in una
`part type: "reasoning"` dedicata: tutto arriva come `type: "text"`, e prima di
questa correzione `_handle_parts` lo pubblicava senza filtro.

La regola 5 nomina già le forme tipiche del difetto («dobbiamo…», «l'utente
vuole…», «quindi rispondo…», "let's answer"): `_e_ragionamento_non_filtrato`
le riconosce, più i frammenti letterali delle regole stesse.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from . import session as S
from .session import OpenCodeChatSession, _e_ragionamento_non_filtrato


def _sessione() -> OpenCodeChatSession:
    s = OpenCodeChatSession.__new__(OpenCodeChatSession)
    s.kind = "segretario"
    s.chat_id = "chan:SEAL-1:acme:segretario"
    s._thinkseam = S._ThinkSeam()
    s._think_n = 0
    s.last_activity = datetime.now(timezone.utc) - timedelta(seconds=60)
    return s


#: Estratto reale (accorciato) del leak segnalato da Davide — comincia in
#: inglese benché il canale sia italiano, e non arriva mai a una risposta.
_ESEMPIO_REALE = (
    "This is a strange context — the reasoning block leaked into the "
    "conversation as if it were the assistant turn from a completely "
    "different task (updating a Tomato S.R.L. topic summary about an "
    "assembly date change). The user wants to update the summary and "
    "potentially the minutes of the topic based on the last message from "
    "davide. Let me refine the summary. Wait, I'll double check the "
    "Prossimi passi I've written. " + ("altro testo di riempimento. " * 20)
)


class TheHeuristicRecognizesTheLeakTests(unittest.TestCase):
    def test_the_reported_example_is_flagged(self) -> None:
        self.assertTrue(_e_ragionamento_non_filtrato(_ESEMPIO_REALE))

    def test_a_literal_fragment_of_the_platform_rule_is_flagged(self) -> None:
        """Regola 5: se ricompare nel testo, il modello sta ragionando SULLE
        proprie istruzioni invece di eseguirle."""
        testo = ("Ok, procedo. " * 30) + (
            "Ricordo che il messaggio deve contenere esclusivamente la "
            "risposta finale destinata all'interlocutore, quindi ora scrivo "
            "solo quello.")
        self.assertTrue(_e_ragionamento_non_filtrato(testo))

    def test_an_italian_planning_opener_is_flagged_too(self) -> None:
        testo = "Allora, l'utente vuole che io aggiorni il summary. " + (
            "Vediamo cosa serve fare passo per passo. " * 15)
        self.assertTrue(_e_ragionamento_non_filtrato(testo))

    def test_a_normal_short_reply_is_not_flagged(self) -> None:
        self.assertFalse(_e_ragionamento_non_filtrato(
            "Fatto: ho aggiornato il summary con la nuova data dell'assemblea "
            "(22 settembre) e le relative voci in Prossimi passi."))

    def test_a_short_reply_that_merely_contains_let_me_is_not_flagged(self) -> None:
        """Il tetto di lunghezza esiste apposta: un «fammi controllare» isolato
        in una risposta corta è conversazione normale, non un pensiero fuori
        posto — senza tetto un falso positivo cancellerebbe una risposta vera."""
        self.assertFalse(_e_ragionamento_non_filtrato(
            "Let me check di nuovo i dati prima di confermare: tutto ok."))

    def test_empty_or_none_is_not_flagged(self) -> None:
        self.assertFalse(_e_ragionamento_non_filtrato(""))
        self.assertFalse(_e_ragionamento_non_filtrato(None))

    def test_a_long_but_ordinary_reply_is_not_flagged(self) -> None:
        """La lunghezza da sola non basta: un resoconto lungo e pulito, che non
        apre con un tell-tale di pianificazione, resta in chat."""
        testo = ("Riepilogo del lavoro svolto: ho letto i tre documenti "
                 "allegati e confrontato le cifre con lo scadenzario. " * 10)
        self.assertFalse(_e_ragionamento_non_filtrato(testo))


class TheLeakIsRoutedToThinkingNotChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_leaked_block_becomes_a_thinking_chunk(self) -> None:
        s = _sessione()
        eventi = []
        with patch.object(S.bus, "publish", AsyncMock(side_effect=lambda e: eventi.append(e))):
            out = await s._handle_parts({"parts": [{"type": "text", "text": _ESEMPIO_REALE}]})
        self.assertEqual("", out, "il pensiero leaked non deve entrare nella risposta finale")
        tipi = [e.type for e in eventi]
        self.assertIn("thinking_chunk", tipi)
        self.assertNotIn("message_chunk", tipi)

    async def test_a_normal_reply_still_reaches_the_chat(self) -> None:
        s = _sessione()
        eventi = []
        with patch.object(S.bus, "publish", AsyncMock(side_effect=lambda e: eventi.append(e))):
            out = await s._handle_parts(
                {"parts": [{"type": "text", "text": "Fatto: summary aggiornato."}]})
        self.assertEqual("Fatto: summary aggiornato.", out)
        tipi = [e.type for e in eventi]
        self.assertIn("message_chunk", tipi)
        self.assertNotIn("thinking_chunk", tipi)

    async def test_a_real_reasoning_part_still_goes_to_thinking_as_before(self) -> None:
        """Non-regressione: il canale `reasoning` legittimo non deve cambiare
        comportamento — questa correzione riguarda solo i blocchi `text`."""
        s = _sessione()
        eventi = []
        with patch.object(S.bus, "publish", AsyncMock(side_effect=lambda e: eventi.append(e))):
            await s._handle_parts({"parts": [{"type": "reasoning", "text": "penso quindi..."}]})
        self.assertEqual(["thinking_chunk"], [e.type for e in eventi])


if __name__ == "__main__":
    unittest.main()
