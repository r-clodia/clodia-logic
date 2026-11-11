"""#671 — the agent-server's door to the Databases page: who may ask, and
that the person travels to the gateway as a signed on-behalf token."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import databases as D


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._data, self.text = status, data or {"ok": True}, ""

    def json(self):
        return self._data


class _Base(unittest.TestCase):
    def client(self, who, admin_=False, human=True, status=200, data=None):
        app = FastAPI()
        app.include_router(D.router)
        self.sent = []

        def request(method, url, **kw):
            self.sent.append((method, url, kw))
            return _Resp(status, data)
        for p in (patch("server.api.agents._principal_from_request", lambda r: who),
                  patch.object(D.admin, "is_human", lambda n: human),
                  patch.object(D.admin, "is_admin", lambda n: admin_),
                  patch.object(D.gateway_pdp, "_token", lambda p: f"tok-{p}"),
                  patch.object(D.gateway_pdp, "_gw_base", lambda: "http://gw"),
                  patch.object(D.gateway_pdp._gw_http, "request", request)):
            p.start()
            self.addCleanup(p.stop)
        return TestClient(app)


class DoorTests(_Base):
    def test_anonymous_and_agents_are_refused(self):
        self.assertEqual(self.client(None).get("/clodia/databases").status_code, 401)
        self.assertEqual(self.client("clodia", human=False).get("/clodia/databases").status_code,
                         403)

    def test_the_person_travels_as_an_on_behalf_token(self):
        c = self.client("mario")
        r = c.get("/clodia/databases/datastore/tomato-pack/clienti/rows",
                  params={"table": "persone", "scope": "SEAL-2/commerciale", "x": "1"})
        self.assertEqual(r.status_code, 200)
        method, url, kw = self.sent[-1]
        self.assertEqual((method, url), ("GET", "http://gw/internal/databases/datastore/"
                                                "tomato-pack/clienti/rows"))
        self.assertEqual(kw["headers"]["Authorization"], "Bearer tok-mario")
        self.assertEqual(kw["params"], {"table": "persone", "scope": "SEAL-2/commerciale"})

    def test_containers_and_schema_are_admin_only(self):
        c = self.client("mario")
        self.assertEqual(c.post("/clodia/databases", json={"action": "delete"}).status_code, 403)
        self.assertEqual(c.post("/clodia/databases/datastore/a/b/schema",
                                json={"op": "drop_table"}).status_code, 403)
        self.assertEqual(self.sent, [])
        a = self.client("davide", admin_=True)
        self.assertEqual(a.post("/clodia/databases", json={"action": "boh"}).status_code, 400)
        r = a.post("/clodia/databases", json={"action": "delete", "kind": "rag",
                                              "name": "manuali", "confirm": "manuali"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.sent[-1][2]["json"]["confirm"], "manuali")

    def test_row_ops_are_checked_before_forwarding(self):
        c = self.client("mario")
        self.assertEqual(c.post("/clodia/databases/datastore/a/b/rows",
                                json={"op": "drop"}).status_code, 400)
        self.assertEqual(c.post("/clodia/databases/datastore/a/b/rows",
                                json={"op": "insert", "table": "t"}).status_code, 200)

    def test_the_gateway_refusal_is_passed_on(self):
        c = self.client("mario", status=403, data={"error": "non importato nel topic"})
        r = c.get("/clodia/databases/collection/manuali/documents")
        self.assertEqual((r.status_code, r.json()["detail"]), (403, "non importato nel topic"))


class JobScopeTests(_Base):
    """#670 jobs are scopes too: listed for admins, cleaned on delete — on the
    real scheduler records (`db.set_job_data`), in a temp jobs dir."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from ..scheduler import db
        self.db = db
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(db, "JOBS_DIR", Path(tmp.name))
        p.start()
        self.addCleanup(p.stop)
        j7 = db.create_job("sette", "0 8 * * *", "p")
        db.create_job("otto", "0 9 * * *", "p")
        db.set_job_data(j7["id"], [{"kind": "rag", "name": "manuali"},
                                   {"kind": "datastore", "name": "a/b"}])
        self.j7 = j7["id"]

    def test_admins_see_job_scopes_in_the_catalogue(self):
        data = {"is_admin": True, "entries": [
            {"key": "rag:manuali", "imported_by_scopes": ["SEAL-2/commerciale"]},
            {"key": "datastore:x/y", "imported_by_scopes": []}]}
        out = self.client("davide", admin_=True, data=data).get("/clodia/databases").json()
        self.assertEqual(out["entries"][0]["imported_by_scopes"],
                         ["SEAL-2/commerciale", f"job:{self.j7}"])
        self.assertEqual(out["entries"][1]["imported_by_scopes"], [])
        data["is_admin"] = False
        data["entries"][0]["imported_by_scopes"] = ["SEAL-2/commerciale"]
        out = self.client("mario", data=data).get("/clodia/databases").json()
        self.assertEqual(out["entries"][0]["imported_by_scopes"], ["SEAL-2/commerciale"])

    def test_delete_removes_the_resource_from_job_imports(self):
        c = self.client("davide", admin_=True, data={"ok": True, "imports_removed": ["SEAL-2/x"],
                                                     "imports_failed": []})
        r = c.post("/clodia/databases", json={"action": "delete", "kind": "rag",
                                              "name": "manuali", "confirm": "manuali"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.sent[-1][2]["json"]["job_scopes"], [f"job:{self.j7}"])
        self.assertEqual(self.db.get_job(self.j7)["data"], [{"kind": "datastore", "name": "a/b"}])
        self.assertEqual(r.json()["imports_removed"], ["SEAL-2/x", f"job:{self.j7}"])

    def test_a_refused_gateway_delete_touches_no_job(self):
        c = self.client("davide", admin_=True, status=403, data={"error": "clearance"})
        r = c.post("/clodia/databases", json={"action": "delete", "kind": "rag",
                                              "name": "manuali", "confirm": "manuali"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(len(self.db.get_job(self.j7)["data"]), 2)


class UploadTests(_Base):
    def test_the_file_is_forwarded_raw_with_its_name(self):
        c = self.client("mario")
        r = c.post("/clodia/databases/collection/manuali/upload",
                   files={"file": ("Guida.pptx", b"PK\x03\x04", "application/octet-stream")},
                   data={"doc_name": "Guida", "scope": "SEAL-2/commerciale"})
        self.assertEqual(r.status_code, 200, r.text)
        method, url, kw = self.sent[-1]
        self.assertTrue(url.endswith("/internal/databases/collection/manuali/upload"))
        self.assertEqual(kw["data"], b"PK\x03\x04")
        self.assertEqual(kw["params"], {"filename": "Guida.pptx", "doc_name": "Guida",
                                        "version": "1", "scope": "SEAL-2/commerciale"})

    def test_an_oversized_file_stops_here(self):
        c = self.client("mario")
        with patch.object(D, "_upload_max", lambda: 4):
            r = c.post("/clodia/databases/collection/manuali/upload",
                       files={"file": ("a.txt", b"12345", "text/plain")})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self.sent, [])

    def test_the_limit_holds_before_the_multipart_is_parsed(self):
        c = self.client("mario")
        parsed = []
        from starlette.requests import Request as R
        real = R.form

        def spy(self, *a, **k):
            parsed.append(1)
            return real(self, *a, **k)
        with patch.object(D, "_upload_max", lambda: 10), patch.object(R, "form", spy):
            # A declared Content-Length above the ceiling: refused unread.
            r = c.post("/clodia/databases/collection/manuali/upload", content=b"x" * 10,
                       headers={"Content-Length": str(10 * 1024 * 1024),
                                "Content-Type": "multipart/form-data; boundary=b"})
            self.assertEqual(r.status_code, 413)
            # No Content-Length (chunked): the streaming cap stops it.
            def gen():
                for _ in range(200):
                    yield b"y" * 1024
            r = c.post("/clodia/databases/collection/manuali/upload", content=gen(),
                       headers={"Content-Type": "multipart/form-data; boundary=b"})
            self.assertEqual(r.status_code, 413)
        self.assertEqual((parsed, self.sent), ([], []))


class SurfaceTests(unittest.TestCase):
    def test_mutations_are_audited_as_control_plane(self):
        from . import audit_mw
        self.assertEqual(audit_mw.kind_of("POST", "/clodia/databases/collection/x/upload"),
                         "database")

    def test_platform_is_a_reserved_pack_name(self):
        from . import plugin_import
        with self.assertRaises(plugin_import.PluginImportError):
            plugin_import._sanitize_plugin_name("platform")


if __name__ == "__main__":
    unittest.main()
