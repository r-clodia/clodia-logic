from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from . import agents


class _FakeSession:
    def __init__(self, data: dict) -> None:
        self._data = data

    @property
    def last_activity(self):
        return self._data["last_activity"]

    def to_dict(self) -> dict:
        return self._data


class _FakeManager:
    def __init__(self, rows: list[dict]) -> None:
        self._sessions = [_FakeSession(row) for row in rows]

    def list(self):
        return self._sessions


class RuntimeSessionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._orig_manager = agents.manager
        self._orig_spawns_root = agents.SPAWNS_ROOT
        self._tmp = TemporaryDirectory()
        agents.SPAWNS_ROOT = Path(self._tmp.name)

    async def asyncTearDown(self) -> None:
        agents.manager = self._orig_manager
        agents.SPAWNS_ROOT = self._orig_spawns_root
        self._tmp.cleanup()

    async def test_live_session_exposes_spawn_identity(self) -> None:
        agents.manager = _FakeManager([{
            "chat_id": "chan:SEAL-1:hedge-iot:clodia",
            "kind": "clodia",
            "runtime": "claude",
            "principal": "owner",
            "status": "idle",
            "last_activity": "2026-07-01T10:00:00+00:00",
            "created_at": "2026-07-01T09:00:00+00:00",
            "total_tokens": {"input": 10, "output": 4, "runs": 1},
            "spawn_id": "clodia-5",
            "spawn_instance": "5",
        }])

        res = await agents.runtime_sessions()

        self.assertEqual(len(res["sessions"]), 1)
        row = res["sessions"][0]
        self.assertEqual(row["agent"], "clodia")
        self.assertEqual(row["spawn_id"], "clodia-5")
        self.assertEqual(row["spawn_instance"], "5")
        self.assertEqual(row["state"], "idle")

    async def test_materialized_spawn_without_manager_session_stays_idle(self) -> None:
        (agents.SPAWNS_ROOT / "ophelia-7" / "scratch").mkdir(parents=True)
        agents.manager = _FakeManager([])

        res = await agents.runtime_sessions()

        self.assertEqual(len(res["sessions"]), 1)
        row = res["sessions"][0]
        self.assertEqual(row["chat_id"], "spawn:ophelia-7")
        self.assertEqual(row["agent"], "ophelia")
        self.assertEqual(row["spawn_id"], "ophelia-7")
        self.assertEqual(row["spawn_instance"], "7")
        self.assertEqual(row["state"], "idle")


if __name__ == "__main__":
    unittest.main()
