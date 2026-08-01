"""Issue #86 — la working directory di spawn può sparire sotto una sessione viva.

Invarianti sotto test:
1. riaprire il client con un `cwd` inesistente NON deve morire con
   "Working directory does not exist": lo spawn va rimaterializzato dal seed;
2. un turno su una sessione con client None deve fallire con un errore CHIARO
   (`SessionUnavailable`), non con `'NoneType' object has no attribute 'query'`;
3. la sessione irrecuperabile va sfilata dal manager, così il messaggio dopo
   riparte pulito invece di riprendere lo stesso oggetto morto;
4. lo sweep degli spawn orfani non deve poter rimuovere il cwd configurato di una
   sessione registrata.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import session as S
from .session import ChatSession, SessionUnavailable
from ..core.models import ClodiaStatus


def _make_session(cwd: str) -> ChatSession:
    sess = ChatSession.__new__(ChatSession)  # bypassa known_kind()
    sess.chat_id = "chan:SEAL-1:bando-camcom-2026:avvocato"
    sess.kind = "avvocato"
    sess.title = "test"
    sess.status = ClodiaStatus.IDLE
    sess._client = None
    sess._client_ctx = None
    sess._lock = asyncio.Lock()
    sess._current_turn_task = None
    sess._last_event_at = 0.0
    sess._watchdog_fired = False
    sess._last_usage = {}
    sess._total_tokens = {"input": 0, "output": 0, "runs": 0}
    sess._spawn = None
    sess._sandbox_uid = None
    sess._opts_kwargs = {"cwd": cwd, "env": {}}
    sess._runtime_override = {}
    sess.principal = "davide"
    sess._token_principal = None
    return sess


class EnsureSpawnDirTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_spawn_dir_is_rematerialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            gone = Path(tmp) / "spawns" / "avvocato-1"      # mai creata: sparita
            fresh = Path(tmp) / "spawns" / "avvocato-2"
            (fresh).mkdir(parents=True)
            (fresh / "system-prompt.md").write_text("PROMPT NUOVO", encoding="utf-8")
            sess = _make_session(str(gone))
            spawn = mock.Mock(dir=fresh)
            with mock.patch.object(S, "_materialize_spawn",
                                   return_value=(spawn, fresh)) as mat, \
                 mock.patch.object(S.activity_log, "append"):
                recreated = await sess._ensure_spawn_dir()
            self.assertTrue(recreated)
            mat.assert_called_once()
            self.assertEqual(sess._opts_kwargs["cwd"], str(fresh))
            self.assertEqual(sess._opts_kwargs["system_prompt"], "PROMPT NUOVO")
            self.assertIs(sess._spawn, spawn)

    async def test_existing_spawn_dir_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = _make_session(tmp)
            with mock.patch.object(S, "_materialize_spawn") as mat:
                self.assertFalse(await sess._ensure_spawn_dir())
            mat.assert_not_called()

    async def test_home_and_ownership_follow_the_new_spawn(self):
        """Con la sandbox attiva HOME e uid:gid vanno riapplicati allo spawn
        nuovo, altrimenti il wrapper non-root non può scriverci."""
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / "avvocato-2"
            fresh.mkdir()
            sess = _make_session(str(Path(tmp) / "avvocato-1"))
            sess._opts_kwargs["env"] = {"HOME": str(Path(tmp) / "avvocato-1")}
            sess._sandbox_uid = 60005
            with mock.patch.object(S, "_materialize_spawn",
                                   return_value=(mock.Mock(dir=fresh), fresh)), \
                 mock.patch.object(S, "_seed_gid", return_value=62691), \
                 mock.patch.object(S.activity_log, "append"), \
                 mock.patch("subprocess.run") as run:
                await sess._ensure_spawn_dir()
            self.assertEqual(sess._opts_kwargs["env"]["HOME"], str(fresh))
            cmds = [c.args[0] for c in run.call_args_list]
            self.assertIn(["chown", "-R", "60005:62691", str(fresh)], cmds)
            self.assertIn(["chmod", "-R", "700", str(fresh)], cmds)

    async def test_open_client_ensures_the_dir_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = _make_session(str(Path(tmp) / "gone-1"))
            with mock.patch.object(sess, "_ensure_spawn_dir",
                                   new=mock.AsyncMock(return_value=True)) as ensure, \
                 mock.patch.object(S, "ClaudeAgentOptions", lambda **kw: kw), \
                 mock.patch.object(S, "ClaudeSDKClient") as client_cls, \
                 mock.patch.object(sess, "_set_status", new=mock.AsyncMock()):
                client_cls.return_value.__aenter__ = mock.AsyncMock(return_value="CLIENT")
                await sess._open_client()
            ensure.assert_awaited_once()
            self.assertEqual(sess._client, "CLIENT")


class UnrecoverableSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_on_dead_client_raises_clear_error(self):
        """Scenario del log di #86: la sessione entra nel turno con un client
        valido, il recovery scattato dal refresh token fallisce e lo azzera. Prima
        si proseguiva fino a `None.query(...)`."""
        sess = _make_session("/tmp")
        sess._client = object()

        async def _failed_recovery():
            sess._client = None      # client chiuso ma non riaperto
            return False

        with mock.patch.object(sess, "_refresh_provider_env", return_value=True), \
             mock.patch.object(sess, "_refresh_mcp_principal", return_value=False), \
             mock.patch.object(sess, "_recover_session", new=_failed_recovery), \
             mock.patch.object(sess, "_record", new=mock.AsyncMock()), \
             mock.patch.object(sess, "_publish_error", new=mock.AsyncMock()) as pub, \
             mock.patch.object(sess, "_set_status", new=mock.AsyncMock()), \
             mock.patch.object(S.activity_log, "append"), \
             mock.patch.object(S.manager, "forget") as forget:
            with self.assertRaises(SessionUnavailable) as ctx:
                await sess.send_user_message("ciao")
        self.assertNotIn("NoneType", str(ctx.exception))
        self.assertIn("infrastruttura", str(ctx.exception))
        pub.assert_awaited()
        self.assertEqual(pub.await_args.kwargs.get("reason"), "session_unrecoverable")
        forget.assert_called_once_with(sess.chat_id)

    def test_forget_drops_only_the_dead_session(self):
        mgr = S.ChatManager()
        mgr._chats = {"a": object(), "b": object()}
        self.assertTrue(mgr.forget("a"))
        self.assertFalse(mgr.forget("a"))       # idempotente
        self.assertEqual(list(mgr._chats), ["b"])


class LiveSpawnDirsTests(unittest.TestCase):
    def test_configured_cwd_is_protected_from_the_sweep(self):
        mgr = S.ChatManager()
        sess = _make_session("/datadir/spawns/avvocato-1")
        mgr._chats = {sess.chat_id: sess}
        self.assertIn("/datadir/spawns/avvocato-1", mgr.live_spawn_dirs())


if __name__ == "__main__":
    unittest.main()