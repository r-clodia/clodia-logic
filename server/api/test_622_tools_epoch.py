"""#622 — a session whose tools changed is rebuilt before the next turn."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from . import channels as C


class _Manager:
    def __init__(self, chats):
        self.chats = set(chats)
        self.cooled = []

    def get(self, chat_id):
        if chat_id not in self.chats:
            raise KeyError(chat_id)
        return object()

    async def cool(self, chat_id, motivo):
        self.cooled.append(chat_id)
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


if __name__ == "__main__":
    unittest.main()
