"""clodia-platform#684 on the agent-server side (#685, #687).

- the manifest sanitiser takes datastore STRUCTURES (`{name, schema,
  migrations?, purpose?, pii?, backup?, tier_hint?}`), keeps the legacy
  `path:` entries readable, and drops `path:` from a structure;
- the import copies the schema and migrations next to the manifest, where the
  gateway reads them; a structure whose schema is not in the pack is dropped;
- removing the plugin archives its instances (`datastores/<plugin>/`) with it;
- a skill's `<DATASTORE:name>` token for a structure becomes the structure id;
- an install/update asks the gateway to migrate every instance and reports it;
- `/clodia/databases` forwards `create_instance` and the structures listing;
- the job imports of a legacy datastore follow it to its instance.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from . import plugin_import as PI
from .plugin_import import _sanitize_datastores


class SanitizeStructureTests(unittest.TestCase):
    def test_a_structure_round_trips(self):
        out = _sanitize_datastores([{
            "name": "contabilita", "schema": "mcp/schema.sql", "migrations": "mcp/migrations",
            "purpose": "Contabilità", "pii": True, "backup": True, "tier_hint": "seal-2"}])
        self.assertEqual(out, [{"name": "contabilita", "schema": "mcp/schema.sql",
                                "purpose": "Contabilità", "pii": True, "backup": True,
                                "migrations": "mcp/migrations", "tier_hint": "SEAL-2"}])

    def test_a_structure_ships_no_file(self):
        out = _sanitize_datastores([{"name": "contabilita", "schema": "mcp/schema.sql",
                                     "path": "data/contabilita.db"}])
        self.assertNotIn("path", out[0])

    def test_malformed_structures_are_dropped(self):
        for bad in ({"schema": "mcp/schema.sql"},                       # no name
                    {"name": "Contabilità", "schema": "mcp/schema.sql"},
                    {"name": "x:y", "schema": "mcp/schema.sql"},
                    {"name": "c", "schema": "/etc/schema.sql"},
                    {"name": "c", "schema": "../schema.sql"},
                    {"name": "c", "schema": "mcp/schema.txt"},
                    {"name": "c", "schema": "s.sql", "migrations": "../m"}):
            with self.subTest(bad=bad):
                self.assertEqual([], _sanitize_datastores([bad]))

    def test_unknown_tier_hint_is_dropped(self):
        out = _sanitize_datastores([{"name": "c", "schema": "s.sql", "tier_hint": "SEAL-9"}])
        self.assertNotIn("tier_hint", out[0])

    def test_legacy_entries_stay_readable(self):
        out = _sanitize_datastores([{"path": "data/contabilita.db", "pii": True},
                                    {"name": "c", "schema": "s.sql"}])
        self.assertEqual(["data/contabilita.db", None], [d.get("path") for d in out])


class _Datadir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.plugins = self.data / "plugins"
        self.plugins.mkdir()
        for p in (patch.object(PI, "PLUGINS_META_DIR", self.plugins),):
            p.start()
            self.addCleanup(p.stop)

    def pack_source(self, *, schema=True, migrations=True, decl=None):
        src = Path(tempfile.mkdtemp(prefix="pk684-", dir=self.tmp.name))
        (src / ".claude-plugin").mkdir()
        (src / "mcp").mkdir()
        (src / "mcp" / "server.py").write_text("print('x')\n")
        if schema:
            (src / "mcp" / "schema.sql").write_text("CREATE TABLE t (x);\n")
        if migrations:
            (src / "mcp" / "migrations").mkdir()
            (src / "mcp" / "migrations" / "001_add.sql").write_text("ALTER TABLE t ADD y;\n")
        (src / "skills" / "s").mkdir(parents=True)
        (src / "skills" / "s" / "SKILL.md").write_text("---\nname: s\n---\nDB <DATASTORE:contabilita>\n")
        manifest = {"name": "assetti-contabili", "version": "1.0.0",
                    "mcpServers": {"contabilita": {"command": "python3",
                                                   "args": ["${CLAUDE_PLUGIN_ROOT}/mcp/server.py"]}},
                    "datastores": decl if decl is not None else [
                        {"name": "contabilita", "schema": "mcp/schema.sql",
                         "migrations": "mcp/migrations", "pii": True}]}
        (src / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
        return src


class InstallAndArchiveTests(_Datadir):
    def install(self, src):
        with patch.object(PI, "_install_skill_into_plugin", lambda d, p: d.name), \
                patch.object(PI.catalog, "_invalidate", lambda k: None):
            return PI.install_plugin_from_root(src, source="test")

    def test_schema_and_migrations_land_in_the_dedicated_folder(self):
        out = self.install(self.pack_source())
        self.assertEqual("contabilita", out["datastores"][0]["name"])
        root = self.plugins / "assetti-contabili"
        self.assertTrue((root / ".structures/contabilita/schema.sql").is_file())
        self.assertTrue((root / ".structures/contabilita/migrations/001_add.sql").is_file())
        meta = yaml.safe_load((root / "plugin.yaml").read_text())
        self.assertEqual(".structures/contabilita/schema.sql", meta["datastores"][0]["schema"])
        self.assertEqual(".structures/contabilita/migrations",
                         meta["datastores"][0]["migrations"])

    def test_schema_outside_mcp_is_copied_too(self):
        src = self.pack_source(decl=[{"name": "c", "schema": "db/schema.sql",
                                      "migrations": "db/migrations"}])
        (src / "db" / "migrations").mkdir(parents=True)
        (src / "db" / "schema.sql").write_text("CREATE TABLE a (x);\n")
        (src / "db" / "migrations" / "001_x.sql").write_text("SELECT 1;\n")
        self.install(src)
        root = self.plugins / "assetti-contabili"
        self.assertTrue((root / ".structures/c/schema.sql").is_file())
        self.assertTrue((root / ".structures/c/migrations/001_x.sql").is_file())

    def test_a_manifest_cannot_make_the_update_wipe_the_plugin(self):
        """`migrations: "."` or `"data"` (review of #608): never an rmtree of
        the plugin's folder or of its legacy data."""
        self.install(self.pack_source())
        root = self.plugins / "assetti-contabili"
        (root / "data").mkdir()
        (root / "data" / "contabilita.db").write_bytes(b"BOOKS")
        for mig in (".", "./", "data"):
            with self.subTest(migrations=mig):
                src = self.pack_source(decl=[{"name": "contabilita", "schema": "mcp/schema.sql",
                                              "migrations": mig}])
                (src / "data").mkdir(exist_ok=True)
                (src / "data" / "x.sql").write_text("SELECT 1;\n")
                self.install(src)
                self.assertEqual(b"BOOKS", (root / "data" / "contabilita.db").read_bytes())
                self.assertTrue((root / "plugin.yaml").is_file())
                self.assertTrue((root / "mcp" / "server.py").is_file())
        self.assertEqual([], _sanitize_datastores([{"name": "c", "schema": "s.sql",
                                                    "migrations": "."}]))

    def test_env_colliding_structures_are_dropped(self):
        out = _sanitize_datastores([{"name": "a-b", "schema": "s.sql"},
                                    {"name": "a_b", "schema": "s.sql"}])
        self.assertEqual(["a-b"], [d["name"] for d in out])

    def test_a_structure_without_its_schema_file_is_dropped(self):
        out = self.install(self.pack_source(schema=False))
        self.assertEqual([], out["datastores"])

    def test_removing_the_plugin_archives_its_instances(self):
        self.install(self.pack_source())
        inst = self.data / "datastores" / "assetti-contabili" / "contabilita"
        inst.mkdir(parents=True)
        (inst / "tomato.db").write_bytes(b"SQLite format 3\x00tomato")
        (inst / "uncommon.db").write_bytes(b"SQLite format 3\x00uncommon")
        with patch.object(PI.catalog, "DATA_SKILLS_DIR", self.data / "skills"), \
                patch.object(PI.catalog, "DATA_RULES_DIR", self.data / "rules"), \
                patch.object(PI.catalog, "_invalidate", lambda k: None):
            res = PI.remove_plugin("assetti-contabili")
        self.assertFalse((self.data / "datastores" / "assetti-contabili").exists())
        self.assertEqual([], res["datastores_retained"])
        rows = {r.get("instance"): r for r in res["datastores_archived"] if r.get("instance")}
        self.assertEqual({"assetti-contabili/contabilita:tomato",
                          "assetti-contabili/contabilita:uncommon"}, set(rows))
        archived = Path(rows["assetti-contabili/contabilita:tomato"]["archived"])
        self.assertEqual(b"SQLite format 3\x00tomato", archived.read_bytes())
        self.assertTrue(rows["assetti-contabili/contabilita:tomato"]["pii"])
        self.assertIn(str(self.plugins / "assetti-contabili"), res["removed"])
        # schema and migrations are reinstallable, not "undeclared" data
        undeclared = [r for r in res["datastores_archived"] if r.get("undeclared")]
        self.assertEqual([], undeclared)
        # and the archived inventory lists the two instances
        from . import datastores as DS
        with patch.object(PI, "_archive_root", lambda: self.data / "plugins-archive"):
            rows = [r for r in DS._archived_datastores() if r.get("instance")]
        self.assertEqual({"assetti-contabili/contabilita:tomato",
                          "assetti-contabili/contabilita:uncommon"}, {r["instance"] for r in rows})
        self.assertEqual({"archived"}, {r["status"] for r in rows})


class SkillTokenTests(_Datadir):
    def test_structure_token_is_the_structure_id(self):
        from ..agents import skill_sync as S
        d = self.plugins / "assetti-contabili"
        d.mkdir()
        (d / "plugin.yaml").write_text(yaml.safe_dump({"datastores": [
            {"name": "contabilita", "schema": "mcp/schema.sql"}]}))
        (self.plugins / "base-pack").mkdir()
        (self.plugins / "base-pack" / "plugin.yaml").write_text(yaml.safe_dump({"datastores": [
            {"name": "contacts", "path": "data/contacts.db"}]}))
        with patch.object(S, "data_path", side_effect=lambda rel: self.data / rel):
            m = S._datastore_map("assetti-contabili")
        self.assertEqual("assetti-contabili/contabilita", m["contabilita"])
        self.assertTrue(m["contacts"].endswith("base-pack/data/contacts.db"))   # legacy unchanged


class MigrateOnUpdateTests(_Datadir):
    def setUp(self):
        super().setUp()
        d = self.plugins / "assetti-contabili"
        d.mkdir()
        (d / "plugin.yaml").write_text(yaml.safe_dump({"datastores": [
            {"name": "contabilita", "schema": "mcp/schema.sql"}]}))
        (self.plugins / "normattiva").mkdir()
        (self.plugins / "normattiva" / "plugin.yaml").write_text(yaml.safe_dump({"name": "n"}))
        self.result = {"kind": "pack", "pack": "studio-commercialista",
                       "plugins": [{"plugin": "assetti-contabili"}, {"plugin": "normattiva"}]}

    def run_it(self, fake):
        from . import packs
        with patch.object(packs.gateway_admin, "migrate_datastores", fake):
            asyncio.run(packs._migrate_datastores(self.result, "davide"))
        return self.result

    def test_only_plugins_with_structures_are_sent(self):
        sent = []

        def fake(plugins, by=""):
            sent.append((plugins, by))
            return {"migrated": [{"id": "assetti-contabili/contabilita:tomato", "from": 0,
                                  "to": 1}], "failed": [], "current": []}
        out = self.run_it(fake)
        self.assertEqual([(["assetti-contabili"], "davide")], sent)
        self.assertEqual(1, len(out["datastore_migrations"]["migrated"]))
        self.assertNotIn("warnings", out)

    def test_a_failure_is_in_the_summary(self):
        out = self.run_it(lambda p, by="": {"migrated": [], "current": [], "failed": [
            {"id": "assetti-contabili/contabilita:uncommon", "error": "boom"}]})
        self.assertIn("assetti-contabili/contabilita:uncommon", out["warnings"][0])
        self.assertIn("rilancia l'Update", out["warnings"][0])

    def test_an_unreachable_gateway_is_reported_not_raised(self):
        def boom(p, by=""):
            raise ConnectionError("down")
        out = self.run_it(boom)
        self.assertIn("error", out["datastore_migrations"])
        self.assertTrue(out["warnings"])

    def test_nothing_to_do_sends_nothing(self):
        from . import packs
        self.result["plugins"] = [{"plugin": "normattiva"}]
        with patch.object(packs.gateway_admin, "migrate_datastores",
                          side_effect=AssertionError("must not be called")):
            asyncio.run(packs._migrate_datastores(self.result, "davide"))
        self.assertNotIn("datastore_migrations", self.result)


class DoorTests(unittest.TestCase):
    def client(self, who, admin_=False, data=None):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from . import databases as D
        app = FastAPI()
        app.include_router(D.router)
        self.sent = []

        class _R:
            status_code, text = 200, ""

            def json(self_inner):
                return data or {"ok": True}

        def request(method, url, **kw):
            self.sent.append((method, url, kw))
            return _R()
        for p in (patch("server.api.agents._principal_from_request", lambda r: who),
                  patch.object(D.admin, "is_human", lambda n: True),
                  patch.object(D.admin, "is_admin", lambda n: admin_),
                  patch.object(D.gateway_pdp, "_token", lambda p: f"tok-{p}"),
                  patch.object(D.gateway_pdp, "_gw_base", lambda: "http://gw"),
                  patch.object(D.gateway_pdp._gw_http, "request", request),
                  patch.object(D, "_job_scopes",
                               lambda: {"datastore:assetti-contabili/contabilita:tomato":
                                        ["job:3"]})):
            p.start()
            self.addCleanup(p.stop)
        return TestClient(app)

    def test_create_instance_is_forwarded_for_admins(self):
        body = {"action": "create_instance", "structure": "assetti-contabili/contabilita",
                "name": "uncommon", "tier": "SEAL-1"}
        self.assertEqual(403, self.client("mario").post("/clodia/databases", json=body).status_code)
        c = self.client("davide", admin_=True)
        self.assertEqual(200, c.post("/clodia/databases", json=body).status_code)
        method, url, kw = self.sent[-1]
        self.assertEqual(("POST", "http://gw/internal/databases"), (method, url))
        self.assertEqual(body, kw["json"])

    def test_structures_are_forwarded_with_job_counts_for_admins(self):
        data = {"is_admin": True, "caller_clearance": "SEAL-3", "structures": [
            {"id": "assetti-contabili/contabilita", "instances": [
                {"id": "assetti-contabili/contabilita:tomato",
                 "imported_by_scopes": ["SEAL-2/a", "SEAL-2/b"], "imported_count": 2}]}]}
        r = self.client("davide", admin_=True, data=data).get("/clodia/databases/structures")
        self.assertEqual(200, r.status_code)
        self.assertEqual("http://gw/internal/databases/structures", self.sent[-1][1])
        self.assertEqual("Bearer tok-davide", self.sent[-1][2]["headers"]["Authorization"])
        inst = r.json()["structures"][0]["instances"][0]
        self.assertEqual(["SEAL-2/a", "SEAL-2/b", "job:3"], inst["imported_by_scopes"])
        self.assertEqual(3, inst["imported_count"])
        self.assertEqual("SEAL-3", r.json()["caller_clearance"])

    def test_migrate_instance_goes_to_the_service_route(self):
        from . import gateway_admin
        sent = []

        def fake(packs, by="", *, ids=None):
            sent.append((packs, by, ids))
            return {"migrated": [{"id": ids[0]}], "failed": [], "current": []}
        body = {"action": "migrate_instance", "id": "assetti-contabili/contabilita:tomato"}
        with patch.object(gateway_admin, "migrate_datastores", fake):
            self.assertEqual(403, self.client("mario").post("/clodia/databases",
                                                            json=body).status_code)
            r = self.client("davide", admin_=True).post("/clodia/databases", json=body)
            self.assertEqual(200, r.status_code, r.text)
            self.assertEqual(400, self.client("davide", admin_=True).post(
                "/clodia/databases", json={"action": "migrate_instance", "id": "x"}).status_code)
        self.assertEqual([([], "davide", ["assetti-contabili/contabilita:tomato"])], sent)

    def test_instance_key_is_encoded_in_the_detail_routes(self):
        c = self.client("davide", admin_=True)
        c.get("/clodia/databases/datastore/assetti-contabili/contabilita%3Atomato/tables")
        self.assertEqual("http://gw/internal/databases/datastore/assetti-contabili/"
                         "contabilita%3Atomato/tables", self.sent[-1][1])


class JobDataMigrationTests(unittest.TestCase):
    def setUp(self):
        from ..scheduler import db
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(db, "JOBS_DIR", Path(self.tmp.name) / "jobs")
        p.start()
        self.addCleanup(p.stop)
        self.db = db

    def test_dry_run_apply_idempotent(self):
        from ..scheduler import migrate_job_data as M
        j = self.db.create_job("j", "0 8 * * *", "p")
        self.db.set_job_data(j["id"], [{"kind": "datastore", "name": "assetti-contabili/contabilita"},
                                       {"kind": "rag", "name": "kb"}])
        other = self.db.create_job("k", "0 8 * * *", "p")
        mapping = {"datastore:assetti-contabili/contabilita":
                   "datastore:assetti-contabili/contabilita:tomato"}
        rows = M.plan(mapping)
        self.assertEqual([j["id"]], [r["job"] for r in rows])
        self.assertEqual("assetti-contabili/contabilita",
                         self.db.get_job(j["id"])["data"][0]["name"])      # dry run
        M.apply(rows)
        self.assertEqual([{"kind": "datastore", "name": "assetti-contabili/contabilita:tomato"},
                          {"kind": "rag", "name": "kb"}], self.db.get_job(j["id"])["data"])
        self.assertEqual([], self.db.get_job(other["id"])["data"])
        self.assertEqual([], M.plan(mapping))

    def test_map_file_written_by_the_gateway(self):
        from ..scheduler import migrate_job_data as M
        f = Path(self.tmp.name) / "legacy-migration.json"
        f.write_text(json.dumps({"map": {"datastore:a/b": "datastore:a/b:x", "rag:z": "rag:y"}}))
        self.assertEqual({"datastore:a/b": "datastore:a/b:x"}, M.load_map(f))
        self.assertEqual({}, M.load_map(Path(self.tmp.name) / "missing.json"))


if __name__ == "__main__":
    unittest.main()
