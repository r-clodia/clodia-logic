"""clodia-platform#514 · zero events is proof only if the event stream saw the
WHOLE turn.

§2 re-sends a timed-out opencode turn when `_eventi_turno == 0`: no progress
event, so no tool ran, so re-sending repeats nothing. But the counter is fed by
`_drain_events`, which reads `GET /event` over SSE and, when the stream drops,
reconnects after `_OC_EVENT_RETRY_S`. SSE has no replay: whatever opencode
emits during the gap is never seen. A turn that sent a mail while the reader
was reconnecting ends with 0 events — and, before this fix, was re-sent.

These tests run the REAL `_drain_events` and the REAL `_run_turn` against a
fake `opencode serve` that streams events like the real one: an event emitted
while no reader is connected is lost. The retry decision is the real
`api/channels._turno_ritentabile`.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from . import session as S
from ..api import channels
from .test_423_budget_turno import _sessione


class _OpenCodeSSE:
    """A fake `opencode serve`: `/event` is a live SSE stream (no replay),
    `/message` hangs, `/abort` answers."""

    def __init__(self) -> None:
        self._srv = None
        self._stop = asyncio.Event()
        self._lettori: list = []          # writers of the open /event streams
        self.connessioni = 0
        self.port = 0

    async def start(self) -> None:
        self._srv = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._srv.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._stop.set()
        self.drop()
        self._srv.close()
        try:
            await self._srv.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    def emit(self, tipo: str) -> int:
        """Send an event to whoever is listening NOW. Returns how many readers
        received it: 0 = lost, as with the real server."""
        riga = f"data: {json.dumps({'type': tipo})}\n\n".encode()
        for w in self._lettori:
            w.write(riga)
        return len(self._lettori)

    def in_ascolto(self) -> int:
        return len(self._lettori)

    def drop(self) -> None:
        """The stream falls: every open /event connection is closed."""
        for w in self._lettori:
            w.close()
        self._lettori.clear()

    async def _handle(self, reader, writer) -> None:
        try:
            testa = await reader.readuntil(b"\r\n\r\n")
        except Exception:  # noqa: BLE001
            writer.close()
            return
        righe = testa.decode("latin-1").split("\r\n")
        metodo, path = (righe[0].split(" ") + ["", ""])[:2]
        for riga in righe[1:]:
            if riga.lower().startswith("content-length:"):
                try:
                    await reader.readexactly(int(riga.split(":", 1)[1]))
                except Exception:  # noqa: BLE001
                    pass
        if path == "/event":
            self.connessioni += 1
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n"
                         b'data: {"type":"server.connected"}\n\n')
            self._lettori.append(writer)
            return                         # stays open until drop()/stop()
        if path.endswith("/message"):
            await self._stop.wait()        # the turn that never comes back
            writer.close()
            return
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                     b"Content-Length: 2\r\nConnection: close\r\n\r\n{}")
        try:
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        writer.close()


async def _finche(cond, entro: float = 3.0) -> None:
    t0 = asyncio.get_running_loop().time()
    while not cond():
        if asyncio.get_running_loop().time() - t0 > entro:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


class ContinuitaDelloStreamTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self.srv = _OpenCodeSSE()
        await self.srv.start()
        self.addAsyncCleanup(self.srv.stop)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.sess = _sessione(self.srv.port)
        self.sess._proc = SimpleNamespace(returncode=None)   # `opencode serve` alive
        for p in (mock.patch.object(S, "_OC_EVENT_RETRY_S", 0.3),
                  mock.patch.object(S, "_OPENCODE_TURN_TIMEOUT", 1.5),
                  mock.patch.object(S, "_kind_spec", return_value=None),
                  mock.patch.object(S, "_resolve_sessions_dir",
                                    return_value=pathlib.Path(d.name)),
                  mock.patch.object(S.activity_log, "append")):
            p.start()
            self.addCleanup(p.stop)

    async def _avvia_lettore(self) -> None:
        """Start the real reader and wait until the server holds its stream.
        The waits look at the SERVER, not at the session's flags, so the same
        tests run (and fail for behaviour) without the production change."""
        task = asyncio.ensure_future(self.sess._drain_events())
        await _finche(lambda: self.srv.in_ascolto() == 1)
        await asyncio.sleep(0.1)          # the client has read the headers

        async def _ferma():
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass
        self.addAsyncCleanup(_ferma)

    async def _turno(self, durante=None) -> S.OpenCodeTurnTimeout:
        task = asyncio.ensure_future(self.sess._run_turn("manda la mail al cliente"))
        await asyncio.sleep(0.2)
        if durante is not None:
            await durante()
        with self.assertRaises(S.OpenCodeTurnTimeout) as ctx:
            await task
        return ctx.exception

    async def test_un_tool_nel_buco_della_riconnessione_non_si_rimanda(self) -> None:
        """THE BLOCKER, as a test: the stream drops mid-turn, the tool call is
        emitted while nobody listens, the reader reconnects. The count says 0 —
        and before the fix that 0 bought a re-send of a mail already sent."""
        await self._avvia_lettore()

        async def buco():
            self.srv.drop()
            await asyncio.sleep(0.1)       # the reader has seen the EOF
            self.assertEqual(0, self.srv.emit("message.part.updated"),
                             "the tool event must fall in the gap")
            await _finche(lambda: self.srv.in_ascolto() == 1)   # reconnected
            await asyncio.sleep(0.1)
            self.assertEqual(2, self.srv.connessioni)

        err = await self._turno(durante=buco)
        self.assertEqual(0, self.sess._eventi_turno, "the gap really hid the event")
        self.assertIsNone(err.eventi, "a count with a gap is unknown, not zero")
        self.assertFalse(channels._turno_ritentabile(err),
                         "a turn that may have run a tool was re-sent")

    async def test_stream_caduto_e_non_ancora_riaperto_non_si_rimanda(self) -> None:
        """Same gap, but the budget ends before the reconnection: the
        generation did not change, the stream is simply not there."""
        await self._avvia_lettore()

        async def buco():
            with mock.patch.object(S, "_OC_EVENT_RETRY_S", 30.0):
                self.srv.drop()
                await asyncio.sleep(0.15)   # the reader is now sleeping 30s
            self.srv.emit("message.part.updated")

        err = await self._turno(durante=buco)
        self.assertIsNone(err.eventi)
        self.assertFalse(channels._turno_ritentabile(err))

    async def test_senza_stream_all_inizio_del_turno_non_si_rimanda(self) -> None:
        """No reader at all (not yet connected, or never started): nothing was
        observed, so nothing is proven."""
        err = await self._turno()
        self.assertIsNone(err.eventi)
        self.assertFalse(channels._turno_ritentabile(err))

    async def test_stream_continuo_e_zero_eventi_si_rimanda_ancora(self) -> None:
        """The branch that must keep working: the stream stayed open for the
        whole turn and nothing came — the retry of §2 still applies. If this
        were red too, the green above would be for the wrong reason."""
        await self._avvia_lettore()
        err = await self._turno()
        self.assertEqual(1, self.srv.connessioni)
        self.assertEqual(0, err.eventi)
        self.assertTrue(channels._turno_ritentabile(err))

    async def test_stream_continuo_con_un_evento_non_si_rimanda(self) -> None:
        """The real reader does count what it receives on a continuous stream."""
        await self._avvia_lettore()

        async def tool():
            self.assertEqual(1, self.srv.emit("message.part.updated"))
            await _finche(lambda: self.sess._eventi_turno == 1)

        err = await self._turno(durante=tool)
        self.assertEqual(1, err.eventi)
        self.assertFalse(channels._turno_ritentabile(err))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
