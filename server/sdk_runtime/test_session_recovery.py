"""Test del contratto di recovery di ChatSession.

Invariante richiesta: un turno che fallisce (errore, timeout o subprocess
wedged) NON deve lasciare la sessione bloccata. Dopo il fallimento la sessione
deve (1) rilasciare il lock e (2) tornare PRONTA (IDLE) ricreando il client SDK,
così il messaggio successivo parte pulito. Era questa la causa dei canali che
restavano "bloccati" finché non si ricreava il container.
"""
from __future__ import annotations

import asyncio
import unittest
from contextlib import contextmanager
from unittest import mock

from . import session as S
from .session import ChatSession
from ..core.models import ClodiaStatus


@contextmanager
def _dummy_cm(*_a, **_k):
    class _G:
        def update(self, *_a, **_k):
            pass
    yield _G()


def _patch_seams():
    """Neutralizza le dipendenze pesanti (osservabilità, log su disco, bus)
    lasciando intatta la logica di lock/recovery sotto test."""
    return mock.patch.multiple(
        S,
        langfuse_observation=_dummy_cm,
        langfuse_attributes=_dummy_cm,
        trace_io=lambda x: x,
    )


def _make_session() -> ChatSession:
    sess = ChatSession.__new__(ChatSession)  # bypassa known_kind()
    sess.chat_id = "chan:SEAL-1:test:clodia"
    sess.kind = "clodia"
    sess.title = "test"
    sess.status = ClodiaStatus.IDLE
    sess._client = None
    sess._client_ctx = None
    sess._lock = asyncio.Lock()
    sess._current_turn_task = None
    sess._last_usage = {}
    sess._total_tokens = {"input": 0, "output": 0, "runs": 0}
    sess._spawn = None
    sess._opts_kwargs = {"cwd": "/tmp"}  # presente → recovery ammesso
    sess.principal = "davide"
    sess._token_principal = None
    return sess


class RecoverSessionTests(unittest.IsolatedAsyncioTestCase):

    async def test_recover_restarts_client_and_returns_ready(self):
        sess = _make_session()
        old_ctx = mock.AsyncMock()
        sess._client_ctx = old_ctx
        sess._client = object()
        opened = {"n": 0}

        async def fake_open():
            opened["n"] += 1
            sess._client = object()
            sess._client_ctx = mock.AsyncMock()
            sess.status = ClodiaStatus.IDLE

        with mock.patch.object(sess, "_open_client", side_effect=fake_open):
            ok = await sess._recover_session()

        self.assertTrue(ok)
        self.assertEqual(opened["n"], 1)
        old_ctx.__aexit__.assert_awaited()                 # vecchio client chiuso
        self.assertEqual(sess.status, ClodiaStatus.IDLE)   # sessione pronta

    async def test_recover_proceeds_even_if_old_client_teardown_raises(self):
        sess = _make_session()
        ctx = mock.AsyncMock()
        ctx.__aexit__.side_effect = RuntimeError("subprocess già morto")
        sess._client_ctx = ctx

        with mock.patch.object(sess, "_open_client", new=mock.AsyncMock()) as op:
            ok = await sess._recover_session()

        self.assertTrue(ok)
        op.assert_awaited_once()  # nonostante il teardown abbia sollevato

    async def test_recover_without_opts_is_noop(self):
        sess = _make_session()
        sess._opts_kwargs = None
        ok = await sess._recover_session()
        self.assertFalse(ok)

    async def test_recover_failure_returns_false(self):
        sess = _make_session()
        with mock.patch.object(sess, "_open_client",
                               new=mock.AsyncMock(side_effect=RuntimeError("boom"))):
            ok = await sess._recover_session()
        self.assertFalse(ok)


class SendFailureUnblocksTests(unittest.IsolatedAsyncioTestCase):

    async def test_query_failure_recovers_and_frees_lock(self):
        """query() che esplode → la sessione si ripristina, NON resta THINKING,
        e il lock è di nuovo libero per il messaggio successivo."""
        sess = _make_session()

        client = mock.AsyncMock()
        client.query.side_effect = RuntimeError("client wedged")
        sess._client = client

        recovered = {"n": 0}

        async def fake_recover():
            recovered["n"] += 1
            sess.status = ClodiaStatus.IDLE
            return True

        with _patch_seams(), \
             mock.patch.object(sess, "_record", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_publish_error", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_set_status",
                               new=mock.AsyncMock(side_effect=lambda s: setattr(sess, "status", s))), \
             mock.patch.object(sess, "_recover_session", side_effect=fake_recover), \
             mock.patch.object(S.activity_log, "append"):
            with self.assertRaises(Exception):
                await sess.send_user_message("ciao")

        self.assertEqual(recovered["n"], 1)             # recovery invocato
        self.assertEqual(sess.status, ClodiaStatus.IDLE)  # pronta, non THINKING
        self.assertFalse(sess._lock.locked())           # lock libero → niente deadlock


if __name__ == "__main__":
    unittest.main()
