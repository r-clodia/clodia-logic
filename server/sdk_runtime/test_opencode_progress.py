"""Su opencode il progresso di un turno lungo va visto MENTRE accade.

Seguito di A13 (#216). La prima correzione ha spostato `last_activity` sui tre
runtime, ma per opencode nel punto sbagliato: `_handle_parts` gira **quando il
turno è già finito**. Il turno è una sola POST bloccante:

    r = await c.post(f"{base}/session/{sid}/message", json=body)   # dieci minuti
    full = await self._handle_parts(r.json())                      # solo ora

Quindi per messaggero, segretario e minerva `last_activity` continuava a muoversi
soltanto a fine turno, ed è esattamente l'intervallo in cui `_live_status` li
dichiara `blocked` (`thinking` + silenzio > 180s). Metà colonia restava con il
difetto che #216 descrive, sotto una correzione che sembrava fatta.

Il progresso c'è ed è osservabile: `opencode serve` espone `GET /event` (SSE),
lo stesso stream che alimenta la sua TUI. Va però letto con una distinzione, o
si sostituisce un difetto con il suo opposto: sul canale passa anche
`server.heartbeat`, ogni ~10s, **a sessione ferma**. Verificato contro un
`opencode serve` reale il 18 ago 2026:

    data: {"id":"evt_…","type":"server.connected","properties":{}}
    data: {"id":"evt_…","type":"server.heartbeat","properties":{}}
    data: {"id":"evt_…","type":"server.heartbeat","properties":{}}

Toccare il timestamp su quelli lo renderebbe un orologio: direbbe «vivo» per
sempre, anche su un processo davvero appeso, e `blocked` non scatterebbe più.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import unittest
from datetime import datetime, timedelta, timezone

from .session import OpenCodeChatSession


def _sessione() -> OpenCodeChatSession:
    s = OpenCodeChatSession.__new__(OpenCodeChatSession)
    s.kind = "messaggero"
    s.chat_id = "chan:SEAL-1:x:messaggero"
    s._proc = None
    s._base_url = "http://127.0.0.1:1"
    # vecchia quanto basta perché `_live_status` la chiamerebbe `blocked`
    s.last_activity = datetime.now(timezone.utc) - timedelta(seconds=600)
    return s


class ProgressMovesTheTimestampTests(unittest.TestCase):
    def test_a_tool_call_in_the_middle_of_the_turn_is_progress(self) -> None:
        s = _sessione()
        prima = s.last_activity
        self.assertTrue(s._note_event_line(
            'data: {"id":"evt_1","type":"message.part.updated",'
            '"properties":{"part":{"type":"tool","tool":"bash"}}}'))
        self.assertGreater(s.last_activity, prima)

    def test_the_heartbeat_is_not_progress(self) -> None:
        """Il difetto opposto: un timestamp che si muove da solo dice «vivo»
        anche quando il processo è appeso, e spegne il rilevamento."""
        s = _sessione()
        prima = s.last_activity
        self.assertFalse(s._note_event_line(
            'data: {"id":"evt_2","type":"server.heartbeat","properties":{}}'))
        self.assertEqual(s.last_activity, prima)

    def test_the_connection_itself_is_not_progress(self) -> None:
        s = _sessione()
        prima = s.last_activity
        self.assertFalse(s._note_event_line(
            'data: {"id":"evt_3","type":"server.connected","properties":{}}'))
        self.assertEqual(s.last_activity, prima)

    def test_the_stream_frame_is_not_mistaken_for_an_event(self) -> None:
        """Righe vuote, commenti SSE e JSON monco esistono sul canale: nessuna
        di queste è progresso, e nessuna deve rompere il lettore."""
        s = _sessione()
        prima = s.last_activity
        for riga in ("", "   ", ": keep-alive", "event: message",
                     "data: {non-json", 'data: {"properties":{}}'):
            with self.subTest(riga=riga):
                self.assertFalse(s._note_event_line(riga))
        self.assertEqual(s.last_activity, prima)


class TheStreamIsReadWhileTheTurnRunsTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_line_of_the_stream_is_examined(self) -> None:
        s = _sessione()
        prima = s.last_activity
        await s._consume_events(_FakeStream([
            'data: {"id":"e1","type":"server.connected","properties":{}}',
            "",
            'data: {"id":"e2","type":"message.part.updated","properties":{}}',
        ]))
        self.assertGreater(s.last_activity, prima)

    async def test_a_dead_stream_does_not_break_the_session(self) -> None:
        """Leggere il progresso è utile finché non diventa esso stesso un
        guasto: se lo stream cade, il turno prosegue."""
        s = _sessione()
        s._proc = None                      # nessun processo → niente riconnessione
        await s._drain_events()             # non solleva

    async def test_it_can_be_cancelled(self) -> None:
        s = _sessione()

        class _Proc:
            returncode = None

        s._proc = _Proc()
        task = asyncio.create_task(s._drain_events())
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class _FakeStream:
    """Le righe di un SSE, come le consegna `httpx.Response.aiter_lines()`."""

    def __init__(self, righe: list[str]) -> None:
        self._righe = righe

    async def aiter_lines(self):
        for r in self._righe:
            yield r


class ItIsWiredAndItLivesInTheRightClassTests(unittest.TestCase):
    """`async def start`/`stop` compaiono tre volte in `session.py`: un test sul
    testo non distingue la classe. Questo guarda l'albero (stessa ragione di
    `test_opencode_stderr.ItLivesInTheRightClassTests`)."""

    def _albero(self) -> ast.Module:
        src = pathlib.Path(__file__).with_name("session.py").read_text()
        return ast.parse(src)

    def _metodo(self, classe: str, nome: str) -> str:
        for nodo in self._albero().body:
            if isinstance(nodo, ast.ClassDef) and nodo.name == classe:
                m = next(x for x in nodo.body
                         if isinstance(x, ast.AsyncFunctionDef) and x.name == nome)
                return ast.unparse(m)
        self.fail(f"{classe}.{nome} non trovato")

    def test_starting_the_session_starts_the_reader(self) -> None:
        self.assertIn("_events_task", self._metodo("OpenCodeChatSession", "start"))

    def test_stopping_the_session_cancels_it(self) -> None:
        self.assertIn("_events_task", self._metodo("OpenCodeChatSession", "stop"))

    def test_the_task_belongs_to_opencode_only(self) -> None:
        dentro = set()
        for nodo in self._albero().body:
            if not isinstance(nodo, ast.ClassDef):
                continue
            for figlio in ast.walk(nodo):
                if isinstance(figlio, ast.Attribute) and figlio.attr == "_events_task":
                    dentro.add(nodo.name)
        self.assertEqual(dentro, {"OpenCodeChatSession"})


if __name__ == "__main__":
    unittest.main()
