"""clodia-platform#670 — an agentic job is a data scope.

- the job record carries `data: [{kind, name}]`, written only through
  `set_job_data` after the gateway checked the import (`check_import`);
- the job's owner (a person) or an admin imports/removes; a topic trigger or a
  logic job does not import (the trigger keeps its topic's imports);
- a job imports data only once it declares a tier (owner, 11 Nov 2026: no
  fallback to the agent's clearance), and its tier cannot go below, or be
  removed from under, what it imports;
- an agentic run's chat id is `job:<id>:<suffix>`, so the signed `chat` claim
  names the job and the gateway can read its imports.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import api as A, db, scheduler as S
from ..api import topics_client


class _Jobs(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = db.JOBS_DIR
        db.JOBS_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        db.JOBS_DIR = self._old_dir
        self._tmp.cleanup()


class JobRecordTests(_Jobs):
    def test_a_new_job_imports_nothing(self):
        self.assertEqual(db.create_job("j", "0 8 * * *", "p")["data"], [])

    def test_set_job_data_keeps_only_well_formed_entries(self):
        j = db.create_job("j", "0 8 * * *", "p")
        out = db.set_job_data(j["id"], [
            {"kind": "rag", "name": "kb"}, {"kind": "rag", "name": "kb"},
            {"kind": "datastore", "name": " base-pack/contacts "},
            {"kind": "drive", "name": "x"}, {"kind": "rag", "name": ""}, "rag:kb"])
        self.assertEqual(out["data"], [{"kind": "rag", "name": "kb"},
                                       {"kind": "datastore", "name": "base-pack/contacts"}])
        self.assertEqual(db.get_job(j["id"])["data"], out["data"])

    def test_an_older_record_reads_without_data(self):
        (db.JOBS_DIR / "3.yaml").write_text(yaml.safe_dump({
            "id": 3, "name": "vecchio", "cron_expr": "0 8 * * *", "prompt": "p"}))
        self.assertEqual(db.get_job(3)["data"], [])

    def test_an_update_keeps_the_imports(self):
        j = db.create_job("j", "0 8 * * *", "p")
        db.set_job_data(j["id"], [{"kind": "rag", "name": "kb"}])
        db.update_job(j["id"], prompt="altro")
        self.assertEqual(db.get_job(j["id"])["data"], [{"kind": "rag", "name": "kb"}])


class JobDataRouteTests(_Jobs):
    def setUp(self) -> None:
        super().setUp()
        self.sent: list[dict] = []
        self.refuse = None
        self.who, self.admin_, self.human = "davide", False, True

        def catalog(body=None):
            self.sent.append(body)
            if self.refuse:
                raise topics_client.TopicsClientError(
                    "data-catalog: 400", status=400, detail=self.refuse)
            if body.get("action") == "check_scope_tier":
                return {"ok": True, "tier": body["tier"]}
            return {"kind": body["kind"], "name": body["resource"], "tier": "SEAL-1"}
        ps = [patch("server.api.agents._principal_from_request", lambda r: self.who),
              patch("server.api.admin.is_admin", lambda n: self.admin_),
              patch("server.api.admin.is_human", lambda n: self.human),
              patch("server.api.topics._principal_clearance", lambda n: "SEAL-3"),
              patch("server.sdk_runtime.session._effective_clearance",
                    lambda kind, override=None: "SEAL-4"),     # must never be read
              patch.object(topics_client, "data_catalog", catalog)]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)
        app = FastAPI()
        app.include_router(A.router)
        self.c = TestClient(app)
        self.job = db.create_job("digest", "0 8 * * *", "p", agent="clodia", owner="davide",
                                 tier="SEAL-2")

    def post(self, **body):
        return self.c.post(f"/clodia/jobs/{self.job['id']}/data", json=body)

    def test_the_owner_imports_after_the_gateways_check(self):
        r = self.post(action="import", kind="rag", resource="kb")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"], [{"kind": "rag", "name": "kb"}])
        sent = self.sent[-1]
        self.assertEqual((sent["action"], sent["kind"], sent["resource"], sent["scope_kind"]),
                         ("check_import", "rag", "kb", "job"))
        self.assertEqual((sent["by"], sent["by_clearance"]), ("davide", "SEAL-3"))
        self.assertEqual(sent["tier"], "SEAL-2")
        self.assertEqual(r.json()["job_tier"], "SEAL-2")

    def test_no_tier_no_import(self):
        """Owner, 11 Nov 2026: the agent's clearance is no fallback."""
        db.update_job(self.job["id"], tier="")
        r = self.post(action="import", kind="rag", resource="kb")
        self.assertEqual(r.status_code, 400)
        self.assertIn("set the job tier before importing data", r.text)
        self.assertEqual(self.sent, [])                    # the gateway is not even asked
        self.assertEqual(db.get_job(self.job["id"])["data"], [])

    def test_lowering_the_tier_below_the_imports_is_refused(self):
        self.post(action="import", kind="rag", resource="kb")
        self.refuse = "il job importa rag 'kb' (SEAL-2): non può scendere a SEAL-1"
        r = self.c.patch(f"/clodia/jobs/{self.job['id']}", json={"tier": "SEAL-1"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(db.get_job(self.job["id"])["tier"], "SEAL-2")
        self.assertEqual((self.sent[-1]["action"], self.sent[-1]["tier"]),
                         ("check_scope_tier", "SEAL-1"))

    def test_removing_the_tier_while_importing_is_refused(self):
        self.post(action="import", kind="rag", resource="kb")
        r = self.c.patch(f"/clodia/jobs/{self.job['id']}", json={"tier": ""})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(db.get_job(self.job["id"])["tier"], "SEAL-2")

    def test_raising_the_tier_is_checked_and_allowed(self):
        self.post(action="import", kind="rag", resource="kb")
        with patch.object(A.scheduler, "register_job", lambda j: None):
            r = self.c.patch(f"/clodia/jobs/{self.job['id']}", json={"tier": "SEAL-3"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(db.get_job(self.job["id"])["tier"], "SEAL-3")

    def test_a_job_without_imports_changes_tier_freely(self):
        with patch.object(A.scheduler, "register_job", lambda j: None):
            r = self.c.patch(f"/clodia/jobs/{self.job['id']}", json={"tier": ""})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("check_scope_tier", [b.get("action") for b in self.sent])

    def test_a_declared_tier_is_the_jobs_tier(self):
        db.update_job(self.job["id"], tier="SEAL-3")
        self.post(action="import", kind="rag", resource="kb")
        self.assertEqual(self.sent[-1]["tier"], "SEAL-3")

    def test_a_refused_import_writes_nothing(self):
        self.refuse = "rag 'kb' è SEAL-3: non entra in un job SEAL-2"
        r = self.post(action="import", kind="rag", resource="kb")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(db.get_job(self.job["id"])["data"], [])

    def test_remove(self):
        self.post(action="import", kind="rag", resource="kb")
        r = self.post(action="remove", kind="rag", resource="kb")
        self.assertEqual((r.status_code, r.json()["data"]), (200, []))
        self.assertEqual(self.post(action="remove", kind="rag", resource="kb").status_code, 400)

    def test_only_the_owner_or_an_admin(self):
        self.who = "mario"
        self.assertEqual(self.post(action="import", kind="rag", resource="kb").status_code, 403)
        self.admin_ = True
        self.assertEqual(self.post(action="import", kind="rag", resource="kb").status_code, 200)

    def test_an_agent_is_refused(self):
        self.admin_, self.human = True, False
        self.assertEqual(self.post(action="import", kind="rag", resource="kb").status_code, 403)

    def test_bad_requests(self):
        self.assertEqual(self.post(action="grant", kind="rag", resource="kb").status_code, 400)
        self.assertEqual(self.post(action="import", kind="drive", resource="kb").status_code, 400)
        self.assertEqual(self.post(action="import", kind="rag", resource=" ").status_code, 400)

    def test_a_logic_job_does_not_import(self):
        j = db.create_job("logico", "0 8 * * *", "p", owner="davide", mode="logic",
                          plan=[{"verb": "x"}])
        r = self.c.post(f"/clodia/jobs/{j['id']}/data",
                        json={"action": "import", "kind": "rag", "resource": "kb"})
        self.assertEqual(r.status_code, 400)

    def test_a_topic_trigger_is_not_a_job_scope(self):
        """It runs in its topic's channel and keeps that topic's imports."""
        t = db.create_topic_trigger("SEAL-1", "x", "p", agent="clodia",
                                    owner="davide", interval_minutes=30)
        r = self.c.post(f"/clodia/jobs/{t['id']}/data",
                        json={"action": "import", "kind": "rag", "resource": "kb"})
        self.assertEqual(r.status_code, 404)


class JobChatIdTests(_Jobs):
    def test_the_chat_id_names_the_job(self):
        cid = S.job_chat_id(12)
        self.assertTrue(cid.startswith("job:12:"), cid)
        self.assertNotEqual(cid, S.job_chat_id(12))     # one chat per run

    def test_fire_job_creates_the_chat_with_the_job_in_its_id(self):
        j = db.create_job("digest", "0 8 * * *", "p", agent="clodia", owner="davide")
        seen = {}

        class _Chat:
            chat_id = "x"
            title = ""

            def to_dict(self):
                return {}

        async def create(chat_id=None, kind=None, run_id=None, runtime_override=None):
            seen.update(chat_id=chat_id, kind=kind, run_id=run_id)
            c = _Chat()
            c.chat_id = chat_id
            return c

        async def publish(ev):
            return None
        with patch.object(S.manager, "create", create), \
                patch.object(S, "known_kind", lambda k: True), \
                patch.object(S, "_provider_not_conformant", lambda job, agent: None), \
                patch.object(S, "_spawn_run", lambda coro: coro.close()), \
                patch.object(S.bus, "publish", publish), \
                patch("server.agents.pack_state.disabled_agents", lambda: set()):
            out = asyncio.run(S.fire_job(j["id"]))
        self.assertEqual(out["status"], "dispatched", out)
        self.assertTrue(seen["chat_id"].startswith(f"job:{j['id']}:"))
        self.assertEqual(seen["run_id"], f"job:{j['id']}")


if __name__ == "__main__":
    unittest.main()
