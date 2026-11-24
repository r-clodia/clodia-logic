"""clodia-platform#726 — the /topics rows carry the room's inbox address.

The gateway puts it on each row; the agent-server forwards it on the rows the
reader may see at all (owner or participant), and nothing for a room without
an inbox.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from . import topics

ROWS = [
    {"tier": "SEAL-1", "name": "acme", "owner": "davide", "participants": ["davide"],
     "inbox": "clodia+k7q2@example.org"},
    {"tier": "SEAL-1", "name": "senza", "owner": "davide", "participants": ["davide"]},
    {"tier": "SEAL-2", "name": "altrui", "owner": "mario", "participants": ["mario"],
     "inbox": "clodia+zz99@example.org"},
]


class InboxTests(unittest.TestCase):
    def _list(self, me):
        with patch.object(topics.topics_client, "async_list_topics", new=AsyncMock(return_value=ROWS)), \
                patch.object(topics, "_principal_from_request", return_value=me), \
                patch.object(topics.access_log, "all_times", return_value={}):
            return {r["name"]: r for r in asyncio.run(topics.list_topics(object()))}

    def test_the_rooms_inbox_reaches_its_people(self):
        rows = self._list("davide")
        self.assertEqual(rows["acme"]["inbox"], "clodia+k7q2@example.org")
        self.assertIsNone(rows["senza"]["inbox"])

    def test_another_persons_room_is_not_listed_at_all(self):
        self.assertNotIn("altrui", self._list("davide"))


if __name__ == "__main__":
    unittest.main()
