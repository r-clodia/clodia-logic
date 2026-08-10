"""Lo stderr di `opencode serve` va letto, e non solo per curiosità.

Il 10 ago 2026 un turno di messaggero è morto con:

    opencode HTTP 500: {"name":"UnknownError","data":{"message":
    "Unexpected server error. Check server logs for details.","ref":"err_…"}}

Quel consiglio — *check server logs* — era ineseguibile: il processo era avviato
con `stderr=PIPE` e **nessuno leggeva quella pipe**. I log esistevano e li
buttavamo via.

Ma il difetto più grave non è la diagnosi persa. Una pipe che nessuno svuota si
riempie: sono **64 KB** su Linux, non un numero grande per un server che scrive.
Alla prima scrittura oltre quella soglia il processo **si blocca** — e da fuori
si vede un server che smette di rispondere, con HTTP 500 intermittenti, tanto
più probabili quanto più a lungo la sessione resta viva. Che è esattamente il
profilo osservato: 1 turno riuscito e 2 falliti in ventiquattr'ore, sempre
vicino a un «sessione non trovata → ricreo».

`limit=` su `create_subprocess_exec` non c'entra: dimensiona lo StreamReader
nostro, non il buffer del kernel. È il tipo di dettaglio che rende un difetto
invisibile alla lettura del codice — la riga *sembra* aver già affrontato il
problema.
"""
from __future__ import annotations

import asyncio
import unittest

from .session import OpenCodeChatSession


class _FakeStderr:
    """Uno stderr che produce righe e poi finisce."""

    def __init__(self, righe: list[bytes], blocca_dopo: int | None = None):
        self._righe = list(righe)
        self._blocca_dopo = blocca_dopo
        self.lette = 0

    async def readline(self) -> bytes:
        if self._blocca_dopo is not None and self.lette >= self._blocca_dopo:
            await asyncio.sleep(3600)      # sta lì, come una pipe piena
        if not self._righe:
            return b""
        self.lette += 1
        return self._righe.pop(0)


class _FakeProc:
    def __init__(self, stderr):
        self.stderr = stderr
        self.returncode = None


def _sessione() -> OpenCodeChatSession:
    s = OpenCodeChatSession.__new__(OpenCodeChatSession)
    from collections import deque
    from .session import _OC_STDERR_TAIL
    s.kind = "messaggero"
    s.chat_id = "chan:SEAL-1:x:messaggero"
    s._stderr_tail = deque(maxlen=_OC_STDERR_TAIL)
    s._stderr_task = None
    s._proc = None
    return s


class DrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_line_is_read_until_the_process_ends(self):
        """Il punto non è raccogliere: è **svuotare**. Se il drenaggio si
        fermasse a metà, la pipe tornerebbe a riempirsi."""
        s = _sessione()
        err = _FakeStderr([b"riga 1\n", b"riga 2\n", b"riga 3\n"])
        s._proc = _FakeProc(err)
        await s._drain_stderr()
        self.assertEqual(err.lette, 3)
        self.assertEqual(list(s._stderr_tail), ["riga 1", "riga 2", "riga 3"])

    async def test_the_tail_is_bounded(self):
        """Tenere tutto sarebbe un secondo modo di riempire qualcosa. La coda
        serve a spiegare l'ULTIMO errore, non a fare da log."""
        from .session import _OC_STDERR_TAIL
        s = _sessione()
        s._proc = _FakeProc(_FakeStderr([f"r{i}\n".encode() for i in range(500)]))
        await s._drain_stderr()
        self.assertEqual(len(s._stderr_tail), _OC_STDERR_TAIL)
        self.assertEqual(list(s._stderr_tail)[-1], "r499")

    async def test_a_process_without_stderr_is_not_a_crash(self):
        s = _sessione()
        s._proc = _FakeProc(None)
        await s._drain_stderr()          # non solleva
        s._proc = None
        await s._drain_stderr()

    async def test_reading_never_breaks_the_session(self):
        """Leggere i log è utile finché non diventa esso stesso un guasto."""
        class _Rotto:
            async def readline(self):
                raise OSError("descrittore chiuso")

        s = _sessione()
        s._proc = _FakeProc(_Rotto())
        await s._drain_stderr()          # assorbito

    async def test_it_can_be_cancelled(self):
        """Alla chiusura della sessione il task va fermato: un lettore che
        resta appeso a un processo morto è un task che nessuno raccoglie."""
        s = _sessione()
        s._proc = _FakeProc(_FakeStderr([b"a\n"], blocca_dopo=1))
        task = asyncio.create_task(s._drain_stderr())
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class HintTests(unittest.TestCase):
    def test_the_error_carries_the_last_lines(self):
        """«Check server logs for details» è un consiglio ineseguibile se quei
        log li buttiamo. Ora accompagnano l'errore."""
        s = _sessione()
        for r in ("provider scaleway: connection reset", "fatal: session lost"):
            s._stderr_tail.append(r)
        hint = s._stderr_hint()
        self.assertIn("connection reset", hint)
        self.assertIn("session lost", hint)

    def test_no_lines_no_noise(self):
        """Senza righe l'errore resta com'era: un suffisso vuoto attaccato a
        ogni messaggio insegnerebbe a ignorarlo."""
        self.assertEqual(_sessione()._stderr_hint(), "")


if __name__ == "__main__":
    unittest.main()


class ItLivesInTheRightClassTests(unittest.TestCase):
    """Dove sta il codice, non solo cosa fa.

    Il primo tentativo di questa modifica ha messo la cancellazione del task
    dentro `CodexChatSession.stop()` — una classe che quell'attributo non ce
    l'ha — perché `async def stop(self)` compare tre volte nel file e una
    sostituzione testuale ha preso la prima. Risultato: `AttributeError` a ogni
    chiusura di una sessione codex, in produzione.

    È la seconda volta in una settimana che una sostituzione posizionale
    colpisce l'occorrenza sbagliata. Un test che guarda il TESTO non l'avrebbe
    vista: il testo era giusto, era nel posto sbagliato. Questo guarda l'albero.
    """

    def _classe_di(self, attributo: str) -> set:
        import ast
        import pathlib
        src = pathlib.Path(__file__).with_name("session.py").read_text()
        albero = ast.parse(src)
        dentro = set()
        for nodo in albero.body:
            if not isinstance(nodo, ast.ClassDef):
                continue
            for figlio in ast.walk(nodo):
                if isinstance(figlio, ast.Attribute) and figlio.attr == attributo:
                    dentro.add(nodo.name)
        return dentro

    def test_the_stderr_task_belongs_to_opencode_only(self):
        self.assertEqual(self._classe_di("_stderr_task"), {"OpenCodeChatSession"})

    def test_the_tail_belongs_to_opencode_only(self):
        self.assertEqual(self._classe_di("_stderr_tail"), {"OpenCodeChatSession"})

    def test_stopping_an_opencode_session_cancels_the_reader(self):
        import ast
        import pathlib
        src = pathlib.Path(__file__).with_name("session.py").read_text()
        for nodo in ast.parse(src).body:
            if isinstance(nodo, ast.ClassDef) and nodo.name == "OpenCodeChatSession":
                stop = next(m for m in nodo.body
                            if isinstance(m, ast.AsyncFunctionDef) and m.name == "stop")
                self.assertIn("_stderr_task", ast.unparse(stop))
                return
        self.fail("OpenCodeChatSession non trovata")
