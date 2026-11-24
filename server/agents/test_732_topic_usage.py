"""clodia-platform#732 — time and tokens spent per topic over 24h, 7d, 4w.

From the activity log alone: a turn is a `run_started` closed by the next
`run_done`/`run_error` of the same agent in the same chat; it counts in the
windows its END falls in; tokens are the leaderboard's (normalised, double
writes counted once); job and pack runs have no topic.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from . import activity_log as A

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
CHAT = "chan:SEAL-1:moneyviz-2026:clodia"


def _ev(typ, at, chat=CHAT, usage=None, reply="ok"):
    p = {"chat_id": chat}
    if typ == "run_done":
        p.update({"usage": usage or {}, "reply": reply})
    return {"ts": at.isoformat(), "agent": "clodia", "type": typ, "payload": p}


class _Logs(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        p = patch.object(A, "ACTIVITY_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)
        A._topic_usage_cache.clear()

    def write(self, agent, events):
        for e in events:
            day = e["ts"][:10]
            f = self.dir / agent / f"{day}.jsonl"
            f.parent.mkdir(parents=True, exist_ok=True)
            with f.open("a") as fh:
                fh.write(json.dumps(e) + "\n")


class TopicUsageTests(_Logs):
    def test_windows_time_and_tokens(self):
        u = {"input_tokens": 100, "output_tokens": 50,
             "cache_creation_input_tokens": 10, "cache_read_input_tokens": 0}
        self.write("clodia", [
            # 2 hours ago: 90 s, in every window
            _ev("run_started", NOW - timedelta(hours=2)),
            _ev("run_done", NOW - timedelta(hours=2) + timedelta(seconds=90), usage=u),
            # 3 days ago: 30 s, in 7d and 4w
            _ev("run_started", NOW - timedelta(days=3)),
            _ev("run_done", NOW - timedelta(days=3) + timedelta(seconds=30), usage=u, reply="b"),
            # 20 days ago: an error after 10 s, only in 4w, no tokens
            _ev("run_started", NOW - timedelta(days=20)),
            _ev("run_error", NOW - timedelta(days=20) + timedelta(seconds=10)),
            # 40 days ago: outside every window
            _ev("run_started", NOW - timedelta(days=40)),
            _ev("run_done", NOW - timedelta(days=40) + timedelta(seconds=5), usage=u, reply="c"),
        ])
        got = A.topic_usage(now=NOW.timestamp())[("SEAL-1", "moneyviz-2026")]
        self.assertEqual(got["24h"], {"runs": 1, "seconds": 90, "tokens_in": 110, "tokens_out": 50})
        self.assertEqual(got["7d"], {"runs": 2, "seconds": 120, "tokens_in": 220, "tokens_out": 100})
        self.assertEqual(got["4w"], {"runs": 3, "seconds": 130, "tokens_in": 220, "tokens_out": 100})

    def test_a_double_write_counts_once_and_jobs_have_no_topic(self):
        u = {"input_tokens": 5, "output_tokens": 5}
        done = _ev("run_done", NOW - timedelta(minutes=5), usage=u)
        self.write("clodia", [_ev("run_started", NOW - timedelta(minutes=6)), done, done,
                              _ev("run_started", NOW - timedelta(minutes=3), chat="job:4:x"),
                              _ev("run_done", NOW - timedelta(minutes=2), chat="job:4:x", usage=u)])
        got = A.topic_usage(now=NOW.timestamp())
        self.assertEqual(list(got), [("SEAL-1", "moneyviz-2026")])
        self.assertEqual(got[("SEAL-1", "moneyviz-2026")]["24h"]["runs"], 1)

    def test_each_agent_and_chat_pairs_its_own_turns(self):
        other = "chan:SEAL-1:moneyviz-2026:segretario"
        self.write("clodia", [_ev("run_started", NOW - timedelta(minutes=10)),
                              _ev("run_done", NOW - timedelta(minutes=9))])
        self.write("segretario", [_ev("run_started", NOW - timedelta(minutes=10), chat=other),
                                  _ev("run_done", NOW - timedelta(minutes=5), chat=other)])
        got = A.topic_usage(now=NOW.timestamp())[("SEAL-1", "moneyviz-2026")]["24h"]
        self.assertEqual((got["runs"], got["seconds"]), (2, 60 + 300))


class RouteTests(_Logs):
    def _get(self, me, admin=False):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from ..api import agent_registry, topics_client
        self.write("clodia", [_ev("run_started", NOW - timedelta(minutes=10)),
                              _ev("run_done", NOW - timedelta(minutes=9)),
                              _ev("run_started", NOW - timedelta(minutes=8), chat="chan:SEAL-2:altrui:clodia"),
                              _ev("run_done", NOW - timedelta(minutes=7), chat="chan:SEAL-2:altrui:clodia")])
        rows = [{"tier": "SEAL-1", "name": "moneyviz-2026", "owner": "davide", "participants": ["davide"]},
                {"tier": "SEAL-2", "name": "altrui", "owner": "mario", "participants": ["mario"]}]
        app = FastAPI()
        app.include_router(agent_registry.router)
        real = A.topic_usage
        with patch.object(agent_registry, "_principal_from_request", return_value=me), \
                patch.object(agent_registry.admin, "is_admin", return_value=admin), \
                patch.object(topics_client, "async_list_topics", new=AsyncMock(return_value=rows)), \
                patch.object(A, "topic_usage", lambda: real(now=NOW.timestamp())):
            return TestClient(app).get("/api/agents/activity/topics")

    def test_a_person_sees_only_their_rooms(self):
        r = self._get("davide")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([t["name"] for t in r.json()["topics"]], ["moneyviz-2026"])
        self.assertEqual(r.json()["windows"], ["24h", "7d", "4w"])

    def test_an_admin_sees_every_room(self):
        self.assertEqual(sorted(t["name"] for t in self._get("davide", admin=True).json()["topics"]),
                         ["altrui", "moneyviz-2026"])

    def test_anonymous_is_refused(self):
        self.assertEqual(self._get(None).status_code, 401)


if __name__ == "__main__":
    unittest.main()
