"""clodia-platform#738 — processes beside spawns.

A process is deterministic (no LLM): the courier and the topic indexer loops,
and the logic jobs (the backup). Each loop pass is recorded with its outcome,
a failing pass never ends the loop, and the admin sees them all through
`/api/processes`.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import processes as P
from .api import processes_api


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        p = patch.object(P, "PROCESS_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)
        r = patch.dict(P._REGISTRY, {}, clear=True)
        r.start()
        self.addCleanup(r.stop)


class RunRecords(_Base):
    def test_a_pass_is_recorded_with_its_counters(self):
        P.register("courier", "Corriere", 120)

        async def pass_():
            return 3
        self.assertEqual(asyncio.run(P.run("courier", pass_)), 3)
        p = P.get("courier").public()
        self.assertEqual(p["status"], "idle")
        self.assertTrue(p["last_ok"])
        self.assertEqual(p["last_counters"], {"items": 3})
        self.assertIsNotNone(p["next_run_at"])
        lines = list(self.dir.glob("courier/*.jsonl"))
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0].read_text().splitlines()[0])
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["counters"], {"items": 3})

    def test_a_failing_pass_is_failed_and_does_not_raise(self):
        P.register("courier", "Corriere", 120)

        async def boom():
            raise ConnectionError("imap.example.com said no to mario@example.com")
        self.assertIsNone(asyncio.run(P.run("courier", boom)))
        p = P.get("courier").public()
        self.assertEqual(p["status"], "failed")
        self.assertFalse(p["last_ok"])
        # The type only: the message can quote an address.
        self.assertEqual(p["last_error"], "ConnectionError")
        text = next(self.dir.glob("courier/*.jsonl")).read_text()
        self.assertNotIn("mario@example.com", text)

    def test_a_pass_with_errors_inside_is_failed_with_its_counters(self):
        P.register("courier", "Corriere", 120)

        async def partial():
            return {"delivered": 2, "errors": 1, "mailto_errors": 1}
        asyncio.run(P.run("courier", partial))
        p = P.get("courier").public()
        self.assertEqual(p["status"], "failed")
        self.assertEqual(p["last_error"], "1 errori nel giro")
        self.assertEqual(p["last_counters"]["delivered"], 2)

    def test_one_good_pass_clears_failed(self):
        P.register("courier", "Corriere", 120)

        async def boom():
            raise RuntimeError("x")

        async def ok():
            return {"delivered": 1, "fetched": 2, "note": "ignored"}
        asyncio.run(P.run("courier", boom))
        asyncio.run(P.run("courier", ok))
        p = P.get("courier").public()
        self.assertEqual(p["status"], "idle")
        self.assertEqual(p["last_counters"], {"delivered": 1, "fetched": 2})

    def test_a_process_switched_off_is_listed_as_disabled(self):
        P.register("topic-indexer", "Indicizzatore topic", 300, enabled=False)
        rows = P.list_processes([])
        self.assertEqual([(r["name"], r["status"], r["can_run_now"]) for r in rows],
                         [("topic-indexer", "disabled", False)])


class Windows(_Base):
    def _write(self, name, entries):
        d = self.dir / name
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "2026-11-24.jsonl", "a") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_loop_windows_count_runs_and_failures(self):
        now = time.time()
        iso = lambda s: datetime.fromtimestamp(now - s, timezone.utc).isoformat()
        self._write("courier", [
            {"ts": iso(60), "ok": True, "duration_s": 1.5},
            {"ts": iso(3 * 3600), "ok": False, "duration_s": 0.5},
            {"ts": iso(3 * 86400), "ok": True, "duration_s": 2},
            {"ts": iso(20 * 86400), "ok": True, "duration_s": 1},
            {"ts": iso(40 * 86400), "ok": True, "duration_s": 1},   # outside 4w
        ])
        w = P.loop_windows("courier", now)
        self.assertEqual(w["24h"], {"runs": 2, "failures": 1, "seconds": 2.0})
        self.assertEqual(w["7d"]["runs"], 3)
        self.assertEqual(w["4w"]["runs"], 4)

    def test_logic_jobs_are_processes_and_agentic_jobs_are_not(self):
        now = datetime.now(timezone.utc)
        backup = {"id": 7, "name": "backup", "mode": "logic", "enabled": True,
                  "cron_expr": "0 3 * * *", "runs": [
                      {"id": "1", "ts": (now - timedelta(days=1)).isoformat(),
                       "stato": "success", "durata": 40.0},
                      {"id": "2", "ts": (now - timedelta(hours=2)).isoformat(),
                       "stato": "failed", "durata": 3.0, "error": "S3 timeout"}]}
        digest = {"id": 8, "name": "digest", "mode": "agentic", "runs": []}
        rows = P.list_processes([backup, digest],
                                next_fire=lambda i: "2026-11-25T03:00:00+01:00")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["source"], r["job_id"], r["status"]), ("job", 7, "failed"))
        self.assertEqual(r["last_error"], "S3 timeout")
        self.assertEqual(r["next_run_at"], "2026-11-25T03:00:00+01:00")
        self.assertEqual(r["windows"]["24h"], {"runs": 1, "failures": 1, "seconds": 3.0})
        self.assertEqual(r["windows"]["7d"]["runs"], 2)


class WakeUp(_Base):
    def test_request_run_wakes_the_loop_early(self):
        P.register("courier", "Corriere", 120)

        async def go():
            t = asyncio.create_task(P.sleep("courier", 60))
            await asyncio.sleep(0.01)
            self.assertTrue(P.request_run("courier"))
            return await asyncio.wait_for(t, 1)
        self.assertTrue(asyncio.run(go()))

    def test_no_wake_for_a_process_switched_off(self):
        P.register("courier", "Corriere", 120, enabled=False)
        self.assertFalse(P.request_run("courier"))
        self.assertFalse(P.request_run("missing"))


class Api(_Base):
    def _client(self, who, admin_=True, human=True):
        app = FastAPI()
        app.include_router(processes_api.router)
        patches = [
            patch("server.api.caller_auth.caller_principal", return_value=who),
            patch("server.api.admin.is_admin", return_value=admin_),
            patch("server.api.admin.is_human", return_value=human),
            patch.object(processes_api, "_jobs", return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return TestClient(app)

    def test_without_a_caller_401(self):
        self.assertEqual(self._client(None).get("/api/processes").status_code, 401)

    def test_non_admin_or_agent_403(self):
        self.assertEqual(self._client("giovanni", admin_=False)
                         .get("/api/processes").status_code, 403)
        self.assertEqual(self._client("clodia", human=False)
                         .get("/api/processes").status_code, 403)

    def test_admin_lists_and_runs(self):
        P.register("courier", "Corriere", 120)
        c = self._client("davide")
        body = c.get("/api/processes").json()
        self.assertEqual(body["windows"], ["24h", "7d", "4w"])
        self.assertEqual([r["name"] for r in body["processes"]], ["courier"])
        self.assertEqual(c.post("/api/processes/courier/run").json()["requested"], True)
        self.assertEqual(c.post("/api/processes/nope/run").status_code, 404)
        self.assertEqual(c.get("/api/processes/courier/runs").json()["runs"], [])

    def test_run_now_on_a_process_switched_off_409(self):
        P.register("courier", "Corriere", 120, enabled=False)
        self.assertEqual(self._client("davide").post("/api/processes/courier/run")
                         .status_code, 409)


if __name__ == "__main__":
    unittest.main()
