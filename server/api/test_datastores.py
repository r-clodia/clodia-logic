"""Inventario di datastore/collection RAG (pagina "Databases"): attivi,
archiviati (datastore) e orfani (collection RAG).

Davide, 9 set 2026: le due primitive del manifest (`datastores:`,
`rag_collections:`) esistevano già senza un punto che le elencasse tutte
insieme, né un modo di ripulire ciò che resta orfano dopo la disinstallazione
di un pack — che per scelta non cancella mai i dati (`pack_deprovision.py`).
"""
from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import yaml

from ..agents.loader import registry
from . import catalog, datastores, gateway_pdp, pack_import, plugin_import, plugins, rag_store


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _agent_yaml(name: str) -> str:
    return yaml.safe_dump({
        "name": name, "display_name": name.capitalize(), "description": "test",
        "type": "normal", "system_prompt": "system-prompt.md",
        "capabilities": [], "requires_plugins": [],
    }, sort_keys=False)


class DatastoresInventoryTests(unittest.TestCase):
    """Stessa fixture di `test_pack_lifecycle.py`: un pack con datastore + RAG."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.logic_skills = root / "logic-skills"
        self.data_skills = root / "data-skills"
        self.logic_rules = root / "logic-rules"
        self.data_rules = root / "data-rules"
        self.plugins_meta = root / "plugins"
        self.packs_meta = root / "packs"
        self.agents_dir = root / "agents"
        for p in (self.logic_skills, self.data_skills, self.logic_rules,
                  self.data_rules, self.plugins_meta, self.packs_meta,
                  self.agents_dir):
            p.mkdir()

        self._old_catalog = (catalog.LOGIC_SKILLS_DIR, catalog.DATA_SKILLS_DIR,
                             catalog.LOGIC_RULES_DIR, catalog.DATA_RULES_DIR)
        catalog.LOGIC_SKILLS_DIR = self.logic_skills
        catalog.DATA_SKILLS_DIR = self.data_skills
        catalog.LOGIC_RULES_DIR = self.logic_rules
        catalog.DATA_RULES_DIR = self.data_rules
        self._old_plugins_meta = plugin_import.PLUGINS_META_DIR
        plugin_import.PLUGINS_META_DIR = self.plugins_meta
        self._old_packs_meta = pack_import.PACKS_META_DIR
        pack_import.PACKS_META_DIR = self.packs_meta
        self._old_manifest = plugins.EXTERNAL_PACKS_MANIFEST
        plugins.EXTERNAL_PACKS_MANIFEST = root / "external-packs.yaml"
        self._old_agents_dir = registry.base_dir
        registry.base_dir = self.agents_dir
        registry.load()
        self._clear_caches()

        self._old_gw_tool = gateway_pdp.gw_tool
        gateway_pdp.gw_tool = lambda tool, args, principal: (None, (200, {"result": {}}))[1]
        self._old_authz = gateway_pdp.require_authz
        gateway_pdp.require_authz = lambda *a, **k: "davide"
        self._old_rag_list = rag_store.list_collections
        rag_store.list_collections = lambda: []

    def tearDown(self) -> None:
        (catalog.LOGIC_SKILLS_DIR, catalog.DATA_SKILLS_DIR,
         catalog.LOGIC_RULES_DIR, catalog.DATA_RULES_DIR) = self._old_catalog
        plugin_import.PLUGINS_META_DIR = self._old_plugins_meta
        pack_import.PACKS_META_DIR = self._old_packs_meta
        plugins.EXTERNAL_PACKS_MANIFEST = self._old_manifest
        registry.base_dir = self._old_agents_dir
        registry.load()
        gateway_pdp.gw_tool = self._old_gw_tool
        gateway_pdp.require_authz = self._old_authz
        rag_store.list_collections = self._old_rag_list
        self._clear_caches()
        self.tmp.cleanup()

    def _clear_caches(self) -> None:
        for cache in catalog._LIST_CACHE.values():
            cache["ts"] = 0.0
            cache["data"] = None
        for cache in catalog._DETAIL_CACHE.values():
            cache.clear()
        plugins.invalidate_plugins()

    @staticmethod
    def _req():
        return mock.Mock(headers={}, client=None)

    def _install_pack(self) -> None:
        pack_import.import_pack_zip(_zip_bytes({
            "studio/pack.yaml": "name: studio\ndescription: Pack di test\nversion: 1.0.0\n",
            "studio/agents/commercialista/agent.yaml": _agent_yaml("commercialista"),
            "studio/agents/commercialista/system-prompt.md": "# Commercialista\n",
            "studio/plugins/contabilita/plugin.yaml": yaml.safe_dump({
                "name": "contabilita", "description": "Contabilità",
                "mcp_servers": {
                    "contabilita": {"command": "python3", "args": ["mcp/srv.py"]},
                },
                "datastores": [{"path": "db/libri.sqlite", "purpose": "libri contabili",
                                "pii": True}],
                "rag_collections": [{"name": "prassi-fiscale", "resources": []}],
            }),
        }))

    def _con_datastore_scritto(self) -> None:
        """Il datastore esiste davvero sul disco, come lo lascerebbe l'MCP del
        pack alla prima esecuzione — altrimenti l'archiviazione non troverebbe
        nulla da spostare per quel path."""
        db = self.plugins_meta / "contabilita" / "db"
        db.mkdir(parents=True, exist_ok=True)
        (db / "libri.sqlite").write_text("dati del cliente", encoding="utf-8")

    # --- attivi -------------------------------------------------------

    def test_an_installed_pack_shows_its_datastore_as_active(self) -> None:
        self._install_pack()
        self._con_datastore_scritto()
        out = asyncio.run(datastores.list_datastores())
        ds = [d for d in out["datastores"] if d["pack"] == "contabilita"]
        self.assertEqual(1, len(ds))
        self.assertEqual("active", ds[0]["status"])
        self.assertEqual("db/libri.sqlite", ds[0]["path"])
        self.assertTrue(ds[0]["pii"])

    def test_an_installed_pack_shows_its_rag_collection_as_active(self) -> None:
        self._install_pack()
        out = asyncio.run(datastores.list_datastores())
        rc = [r for r in out["rag_collections"] if r["name"] == "prassi-fiscale"]
        self.assertEqual(1, len(rc))
        self.assertEqual("active", rc[0]["status"])
        self.assertEqual("contabilita", rc[0]["pack"])

    # --- archiviati (datastore) ----------------------------------------

    def test_removing_the_plugin_turns_the_datastore_archived(self) -> None:
        self._install_pack()
        self._con_datastore_scritto()
        asyncio.run(plugins.delete_plugin("contabilita", self._req()))

        out = asyncio.run(datastores.list_datastores())
        self.assertEqual([], [d for d in out["datastores"] if d.get("status") == "active"])
        archiviati = [d for d in out["datastores"] if d["status"] == "archived"]
        self.assertEqual(1, len(archiviati))
        self.assertEqual("contabilita", archiviati[0]["pack"])
        self.assertEqual("db/libri.sqlite", archiviati[0]["path"])
        self.assertTrue(archiviati[0]["archive_dir"].startswith("contabilita-"))

    def test_the_orphaned_datastore_can_be_purged(self) -> None:
        self._install_pack()
        self._con_datastore_scritto()
        asyncio.run(plugins.delete_plugin("contabilita", self._req()))
        archive_dir = asyncio.run(datastores.list_datastores())["datastores"][0]["archive_dir"]

        res = asyncio.run(datastores.purge_archived_datastore(archive_dir, self._req()))

        self.assertEqual({"ok": True, "archive_dir": archive_dir}, res)
        self.assertFalse((plugin_import._archive_root() / archive_dir).exists())
        out = asyncio.run(datastores.list_datastores())
        self.assertEqual([], out["datastores"])

    def test_purge_rejects_a_traversal_archive_dir(self) -> None:
        """`archive_dir` arriva dal client: deve restare confinato dentro
        `plugins-archive/`, non usabile per cancellare qualunque altra cosa."""
        for cattivo in ("../etc", "..", "a/../../b", "/etc/passwd", "senza-timestamp"):
            with self.subTest(cattivo=cattivo):
                resp = asyncio.run(datastores.purge_archived_datastore(cattivo, self._req()))
                self.assertEqual(400, resp.status_code)

    def test_purge_of_a_missing_dir_is_404(self) -> None:
        resp = asyncio.run(
            datastores.purge_archived_datastore("fantasma-20260101-000000", self._req()))
        self.assertEqual(404, resp.status_code)

    # --- orfani (collection RAG) ----------------------------------------

    def test_a_collection_with_no_installed_pack_is_orphaned(self) -> None:
        self._install_pack()  # dichiara "prassi-fiscale"
        rag_store.list_collections = lambda: [
            {"collection": "prassi-fiscale", "tier": "SEAL-1", "documents": 3, "chunks": 40},
            {"collection": "vecchio-corpus", "tier": "SEAL-0", "documents": 9, "chunks": 200},
        ]
        out = asyncio.run(datastores.list_datastores())
        nomi_orfani = {r["name"] for r in out["rag_collections"] if r["status"] == "orphaned"}
        self.assertEqual({"vecchio-corpus"}, nomi_orfani)
        # "prassi-fiscale" resta SOLO come attiva (dichiarata dal pack), non
        # anche duplicata come orfana solo perché esiste anche in pgvector.
        stati = [r["status"] for r in out["rag_collections"] if r["name"] == "prassi-fiscale"]
        self.assertEqual(["active"], stati)

    def test_rag_service_unreachable_degrades_to_no_orphans_not_an_error(self) -> None:
        """Un guasto infra sulla sola RAG non deve rompere l'intera pagina: i
        datastore restano leggibili."""
        self._install_pack()
        self._con_datastore_scritto()

        def _boom():
            raise rag_store.RagStoreError("gateway giù")
        rag_store.list_collections = _boom

        out = asyncio.run(datastores.list_datastores())
        self.assertEqual([], [r for r in out["rag_collections"] if r["status"] == "orphaned"])
        self.assertEqual(1, len(out["datastores"]))  # il resto della pagina funziona


if __name__ == "__main__":
    unittest.main()
