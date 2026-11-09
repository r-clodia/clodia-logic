"""#622 — a session whose tools changed is rebuilt before the next turn."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from . import channels as C


class _Manager:
    def __init__(self, chats, evicts=True):
        self.chats = set(chats)
        self.cooled = []
        self.mine = []
        self.evicts = evicts

    def get(self, chat_id):
        if chat_id not in self.chats:
            raise KeyError(chat_id)
        return object()

    async def cool(self, chat_id, motivo, *, mine=False):
        self.cooled.append(chat_id)
        self.mine.append(mine)
        if not self.evicts:
            return False
        self.chats.discard(chat_id)
        return True


class ToolsEpochTests(unittest.TestCase):
    def setUp(self):
        C._TOOLS_EPOCH.clear()
        self.addCleanup(C._TOOLS_EPOCH.clear)
        self.epoch = "e1"
        self.busy = False

    def _run(self, mgr, cid="chan:SEAL-1:x:clodia", fail=False):
        def ep(t, n, a):
            if fail:
                raise C.topics_client.TopicsClientError("giù")
            return self.epoch
        with patch.object(C, "manager", mgr), \
                patch.object(C.topics_client, "tools_epoch", ep), \
                patch.object(C, "_chat_busy", lambda c: self.busy):
            asyncio.run(C._strumenti_della_sessione_aggiornati("SEAL-1", "x", "clodia", cid))

    def test_a_new_session_records_its_epoch_and_a_change_rebuilds_it(self):
        m = _Manager([])
        self._run(m)                                  # about to be created
        m.chats.add("chan:SEAL-1:x:clodia")
        self._run(m)                                  # same epoch: nothing
        self.assertEqual(m.cooled, [])
        self.epoch = "e2"                             # the owner changed the tools
        self._run(m)
        self.assertEqual(m.cooled, ["chan:SEAL-1:x:clodia"])
        self.assertEqual(C._TOOLS_EPOCH["chan:SEAL-1:x:clodia"], "e2")

    def test_a_busy_session_waits_for_the_next_turn(self):
        m = _Manager(["chan:SEAL-1:x:clodia"])
        self._run(m)
        self.epoch, self.busy = "e2", True
        self._run(m)
        self.assertEqual(m.cooled, [])
        self.busy = False
        self._run(m)
        self.assertEqual(m.cooled, ["chan:SEAL-1:x:clodia"])

    def test_a_gateway_that_does_not_answer_changes_nothing(self):
        m = _Manager(["chan:SEAL-1:x:clodia"])
        C._TOOLS_EPOCH["chan:SEAL-1:x:clodia"] = "old"
        self._run(m, fail=True)
        self.assertEqual(m.cooled, [])

    def test_both_channel_dispatchers_check(self):
        import inspect
        self.assertEqual(inspect.getsource(C).count("await _strumenti_della_sessione_aggiornati("), 2)


class OwnReservationTests(unittest.TestCase):
    """#661 — the dispatcher reserves the chat, then asks to rebuild it: its
    own reservation must not shield the session, or the turn runs on the old
    subprocess with the old tools."""

    def setUp(self):
        C._TOOLS_EPOCH.clear()
        self.addCleanup(C._TOOLS_EPOCH.clear)

    def _check(self, mgr, epoch):
        with patch.object(C, "manager", mgr), \
                patch.object(C.topics_client, "tools_epoch", lambda t, n, a: epoch), \
                patch.object(C, "_chat_busy", lambda c: False):
            asyncio.run(C._strumenti_della_sessione_aggiornati(
                "SEAL-1", "x", "clodia", "chan:SEAL-1:x:clodia"))

    def test_the_rebuild_is_asked_as_the_reservation_holder(self):
        mgr = _Manager({"chan:SEAL-1:x:clodia"})
        C._TOOLS_EPOCH["chan:SEAL-1:x:clodia"] = "old"
        self._check(mgr, "new")
        self.assertEqual(mgr.mine, [True])
        self.assertEqual(C._TOOLS_EPOCH["chan:SEAL-1:x:clodia"], "new")

    def test_a_rebuild_that_did_not_happen_is_retried_and_not_logged_as_done(self):
        mgr = _Manager({"chan:SEAL-1:x:clodia"}, evicts=False)
        C._TOOLS_EPOCH["chan:SEAL-1:x:clodia"] = "old"
        with self.assertLogs(C.LOG, level="WARNING") as logs:
            self._check(mgr, "new")
        self.assertEqual(C._TOOLS_EPOCH["chan:SEAL-1:x:clodia"], "old")   # retried next turn
        self.assertIn("NON", "\n".join(logs.output))


class ManagerReservationTests(unittest.TestCase):
    """The real ChatManager: a reservation keeps the reaper away, not its owner;
    a turn in progress keeps everyone away."""

    class _Chat:
        def __init__(self, running=False):
            self.stopped = False
            self._current_turn_task = None
            if running:
                class _T:
                    def done(self):
                        return False
                self._current_turn_task = _T()

        async def stop(self):
            self.stopped = True

        def to_dict(self):
            return {}

    def _manager(self, chat):
        from ..sdk_runtime import session as S
        m = S.ChatManager()
        m._chats["c1"] = chat
        m.reserve("c1")
        return m, S

    def _cool(self, m, S, **kw):
        async def _pub(*a, **k):
            return None
        with patch.object(S.bus, "publish", _pub):
            return asyncio.run(m.cool("c1", "tools_epoch", **kw))

    def test_a_reserved_chat_is_not_evicted_by_others(self):
        chat = self._Chat()
        m, S = self._manager(chat)
        self.assertFalse(self._cool(m, S))
        self.assertFalse(chat.stopped)

    def test_the_reservation_holder_evicts_it(self):
        chat = self._Chat()
        m, S = self._manager(chat)
        self.assertTrue(self._cool(m, S, mine=True))
        self.assertTrue(chat.stopped)
        self.assertNotIn("c1", m._chats)

    def test_a_turn_in_progress_is_never_evicted(self):
        chat = self._Chat(running=True)
        m, S = self._manager(chat)
        self.assertFalse(self._cool(m, S, mine=True))
        self.assertFalse(chat.stopped)


if __name__ == "__main__":
    unittest.main()
