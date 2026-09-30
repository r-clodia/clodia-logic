"""clodia-platform#446 part 2 and #447 part 2 on the agent-server."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ..agents import transcript_retention as tr
from . import audit_admin


class TranscriptRetentionTests(unittest.TestCase):
    def test_each_transcript_follows_the_clock_of_its_tier(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            files = {
                "SEAL-2": root / "clodia" / "chan_SEAL-2_titulon-tech_clodia" / "a.jsonl",
                "SEAL-0": root / "clodia" / "chan_SEAL-0_blog_clodia" / "b.jsonl",
                "other": root / "clodia" / "dm_davide_clodia" / "c.jsonl",
            }
            old = time.time() - 40 * 86400
            for f in files.values():
                f.parent.mkdir(parents=True)
                f.write_text("{}\n")
                os.utime(f, (old, old))
            with patch.dict(os.environ, {"CLODIA_TRANSCRIPT_RETENTION": "SEAL-2=30,SEAL-0=0",
                                         "CLODIA_TRANSCRIPT_RETENTION_OTHER": "60"}):
                removed = tr.apply(root)
            self.assertEqual(removed, {"SEAL-2": 1})
            self.assertFalse(files["SEAL-2"].exists())
            self.assertTrue(files["SEAL-0"].exists())
            self.assertTrue(files["other"].exists())

    def test_the_tier_is_read_from_the_chat_key(self) -> None:
        self.assertEqual(tr.tier_of("chan_SEAL-3_x_avvocato"), "SEAL-3")
        self.assertEqual(tr.tier_of("job_nightly"), "other")


class AuditAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(audit_admin.router)
        self.c = TestClient(app)
        self.sent = {}

        class _Client:
            def __init__(s, *a, **k):
                pass

            async def __aenter__(s):
                return s

            async def __aexit__(s, *a):
                return False

            async def post(s, url, json=None, headers=None):
                self.sent.update(url=url, json=json)
                r = MagicMock()
                r.status_code, r.content = 200, b"TGZ"
                return r

            async def get(s, url, params=None, headers=None):
                self.sent.update(url=url, params=params)
                r = MagicMock()
                r.status_code, r.content, r.json = 200, b"EVIDENCE", (lambda: {"events": 1})
                return r
        p = patch.object(audit_admin.httpx, "AsyncClient", _Client)
        p.start()
        self.addCleanup(p.stop)

    def test_only_an_admin_may_read_the_trail(self) -> None:
        with patch.object(audit_admin, "_principal_from_request", lambda r: None):
            self.assertEqual(self.c.get("/api/admin/audit/status").status_code, 401)
        with patch.object(audit_admin, "_principal_from_request", lambda r: "mario"), \
                patch.object(audit_admin.admin, "is_admin", lambda p: False):
            self.assertEqual(self.c.post("/api/admin/audit/export", json={}).status_code, 403)

    def test_the_export_is_made_in_the_name_of_the_verified_admin(self) -> None:
        with patch.object(audit_admin, "_principal_from_request", lambda r: "davide"), \
                patch.object(audit_admin.admin, "is_admin", lambda p: True):
            r = self.c.post("/api/admin/audit/export",
                            json={"since": "2026-09-01", "purpose": "audit", "reader": "someone-else"})
        self.assertEqual(r.content, b"TGZ")
        self.assertEqual(self.sent["json"]["reader"], "davide")
        self.assertTrue(self.sent["url"].endswith("/internal/audit/export"))

    def test_evidence_carries_the_readers_clearance(self) -> None:
        with patch.object(audit_admin, "_principal_from_request", lambda r: "davide"), \
                patch.object(audit_admin.admin, "is_admin", lambda p: True), \
                patch.object(audit_admin, "_clearance_of", lambda w: "SEAL-3"):
            r = self.c.get("/api/admin/audit/evidence", params={"hash": "sha256:ab", "tier": "SEAL-2"})
        self.assertEqual(r.content, b"EVIDENCE")
        self.assertEqual(self.sent["params"]["clearance"], "SEAL-3")
        self.assertEqual(self.sent["params"]["reader"], "davide")


if __name__ == "__main__":
    unittest.main()
