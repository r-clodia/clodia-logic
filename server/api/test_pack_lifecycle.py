"""Lifecycle dei servizi di un pack: la rimozione deve DISFARE ciò che
l'installazione ha montato (clodia-logic#103).

L'installazione era già simmetrica a metà: `pack_mcp_mount` monta i backend MCP
dichiarati e `pack_ops` riconcilia il resto. La rimozione, no: cancellava file e
lasciava nel gateway i backend del pack e i grant dei suoi agenti — i tool
restavano esposti a chi il pack non ce l'ha più. E il `rmtree` della directory
del plugin portava via anche i **datastore** dichiarati, che stanno per
definizione dentro `plugins/<nome>/`: dati dell'utente cancellati in silenzio da
una disinstallazione.

Questi test misurano le due direzioni: cosa viene smontato e cosa NON viene mai
cancellato.
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
from . import (catalog, gateway_admin, gateway_pdp, pack_import,
               pack_mcp_mount, pack_ops, packs, plugin_import, plugins)


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: una skill\n---\n# {name}\n"


def _agent_yaml(name: str) -> str:
    return yaml.safe_dump({
        "name": name,
        "display_name": name.capitalize(),
        "description": f"Agente di test {name}",
        "type": "normal",
        "system_prompt": "system-prompt.md",
        "capabilities": [],
        "requires_plugins": [],
    }, sort_keys=False)


class PackLifecycleTest(unittest.TestCase):
    """Un pack con MCP, datastore e collection RAG, installato e poi rimosso."""

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

        self._old_catalog = (
            catalog.LOGIC_SKILLS_DIR, catalog.DATA_SKILLS_DIR,
            catalog.LOGIC_RULES_DIR, catalog.DATA_RULES_DIR,
        )
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

        # Nessun test di questo modulo deve poter parlare col gateway vero: chi
        # ne ha bisogno rimpiazza questi doppi con i propri.
        self.gw_calls: list[tuple] = []
        self._old_gw_tool = gateway_pdp.gw_tool
        gateway_pdp.gw_tool = lambda tool, args, principal: (
            self.gw_calls.append((tool, args, principal)) or (200, {"result": {}}))
        self.registered: list[tuple] = []
        self._old_register = gateway_admin.register_agent
        gateway_admin.register_agent = lambda agent, allowed_tools=None, **k: (
            self.registered.append((agent, allowed_tools)) or {"ok": True})
        self._old_authz = gateway_pdp.require_authz
        gateway_pdp.require_authz = lambda *a, **k: "davide"

    def tearDown(self) -> None:
        (
            catalog.LOGIC_SKILLS_DIR, catalog.DATA_SKILLS_DIR,
            catalog.LOGIC_RULES_DIR, catalog.DATA_RULES_DIR,
        ) = self._old_catalog
        plugin_import.PLUGINS_META_DIR = self._old_plugins_meta
        pack_import.PACKS_META_DIR = self._old_packs_meta
        plugins.EXTERNAL_PACKS_MANIFEST = self._old_manifest
        registry.base_dir = self._old_agents_dir
        registry.load()
        gateway_pdp.gw_tool = self._old_gw_tool
        gateway_pdp.require_authz = self._old_authz
        gateway_admin.register_agent = self._old_register
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
    def _admin_request():
        return mock.Mock(headers={}, client=None)

    def _install_pack(self) -> None:
        """Pack `studio` con un plugin che dichiara MCP + datastore + RAG."""
        pack_import.import_pack_zip(_zip_bytes({
            "studio/pack.yaml": "name: studio\ndescription: Pack di test\nversion: 1.0.0\n",
            "studio/agents/commercialista/agent.yaml": _agent_yaml("commercialista"),
            "studio/agents/commercialista/system-prompt.md": "# Commercialista\n",
            "studio/plugins/contabilita/plugin.yaml": yaml.safe_dump({
                "name": "contabilita",
                "description": "Contabilità",
                "mcp_servers": {
                    "contabilita": {"command": "python3", "args": ["mcp/srv.py"]},
                    "normattiva": {"command": "python3", "args": ["mcp/norm.py"]},
                },
                "datastores": [{"path": "db", "purpose": "libri contabili", "pii": True}],
                "rag_collections": [{"name": "prassi-fiscale", "resources": []}],
            }),
            "studio/plugins/contabilita/skills/f24/SKILL.md": _skill_md("f24"),
        }), source="studio.zip")
        self._clear_caches()

    def _seed_datastore(self) -> Path:
        """I dati che l'utente ha accumulato nel datastore del plugin."""
        db = self.plugins_meta / "contabilita" / "db"
        db.mkdir(parents=True, exist_ok=True)
        (db / "libri.sqlite").write_text("dati del cliente", encoding="utf-8")
        return db

    # --- unmount ----------------------------------------------------------

    def test_pack_removal_unmounts_declared_mcp_backends(self) -> None:
        self._install_pack()
        res = asyncio.run(packs.delete_pack("studio", self._admin_request()))

        removed = [args["name"] for tool, args, _p in self.gw_calls
                   if tool == "mcp.remove"]
        self.assertEqual(sorted(removed), ["contabilita", "normattiva"])
        self.assertEqual(sorted(res["deprovision"]["mcp"]["unmounted"]),
                         ["contabilita", "normattiva"])
        self.assertEqual(res["deprovision"]["mcp"]["failed"], [])

    def test_plugin_removal_unmounts_declared_mcp_backends(self) -> None:
        """Stesso difetto dall'altra porta: il plugin si rimuove anche da solo."""
        self._install_pack()
        res = asyncio.run(plugins.delete_plugin("contabilita", self._admin_request()))

        removed = [args["name"] for tool, args, _p in self.gw_calls
                   if tool == "mcp.remove"]
        self.assertEqual(sorted(removed), ["contabilita", "normattiva"])
        self.assertEqual(sorted(res["deprovision"]["mcp"]["unmounted"]),
                         ["contabilita", "normattiva"])

    def test_unmount_failure_does_not_block_removal(self) -> None:
        """Best-effort con report: il gateway che non risponde non deve lasciare
        il pack a metà installato — ma il buco va detto, non ingoiato."""
        self._install_pack()
        gateway_pdp.gw_tool = lambda *_a, **_k: (502, {"detail": "gateway down"})

        res = asyncio.run(packs.delete_pack("studio", self._admin_request()))

        self.assertEqual(res["deleted"], "studio")
        self.assertFalse((self.data_skills / "contabilita").exists())
        failed = res["deprovision"]["mcp"]["failed"]
        self.assertEqual(sorted(f["server"] for f in failed),
                         ["contabilita", "normattiva"])
        self.assertIn("gateway down", failed[0]["error"])

    def test_pack_removal_revokes_agent_grants(self) -> None:
        """L'agente del pack non c'è più: i suoi grant nel gateway nemmeno."""
        self._install_pack()
        self.registered.clear()  # l'installazione registra: qui conta la rimozione

        res = asyncio.run(packs.delete_pack("studio", self._admin_request()))

        self.assertEqual(self.registered, [("commercialista", [])])
        self.assertEqual(res["deprovision"]["grants_revoked"], ["commercialista"])

    # --- dati: mai cancellati in silenzio ---------------------------------

    def test_datastore_survives_plugin_removal(self) -> None:
        """Il datastore sta in `plugins/<nome>/`, che la rimozione cancellava
        con un rmtree: i libri contabili di un cliente sparivano insieme alla
        skill. Ora vengono archiviati, e la risposta dice dove."""
        self._install_pack()
        self._seed_datastore()

        res = asyncio.run(plugins.delete_plugin("contabilita", self._admin_request()))

        archived = res["deprovision"]["datastores_archived"]
        self.assertEqual([a["path"] for a in archived], ["db"])
        dest = Path(archived[0]["archived"])
        self.assertEqual((dest / "libri.sqlite").read_text(encoding="utf-8"),
                         "dati del cliente")
        self.assertFalse((self.plugins_meta / "contabilita").exists())

    def test_datastore_survives_pack_removal(self) -> None:
        self._install_pack()
        self._seed_datastore()

        res = asyncio.run(packs.delete_pack("studio", self._admin_request()))

        archived = res["deprovision"]["datastores_archived"]
        dest = Path(archived[0]["archived"])
        self.assertTrue((dest / "libri.sqlite").is_file())

    def test_rag_collections_are_reported_not_dropped(self) -> None:
        """Le collection RAG non si cancellano da sole: nessun verbo rag.* parte
        dalla rimozione, e restano come gap dichiarato all'admin."""
        self._install_pack()

        res = asyncio.run(packs.delete_pack("studio", self._admin_request()))

        self.assertEqual(res["deprovision"]["rag_collections_kept"], ["prassi-fiscale"])
        self.assertEqual([t for t, _a, _p in self.gw_calls if t.startswith("rag.")], [])

    def test_removal_without_declarations_says_nothing(self) -> None:
        """Un pack di sole skill non deve produrre né chiamate al gateway né
        una sezione `deprovision` vuota da leggere."""
        pack_import.import_pack_zip(_zip_bytes({
            "solo-skill/pack.yaml": "name: solo-skill\ndescription: x\nversion: 1.0.0\n",
            "solo-skill/plugins/helper/skills/hello/SKILL.md": _skill_md("hello"),
        }), source="solo-skill.zip")
        self._clear_caches()

        res = asyncio.run(packs.delete_pack("solo-skill", self._admin_request()))

        self.assertNotIn("deprovision", res)
        self.assertEqual(self.gw_calls, [])


class DriftTest(unittest.TestCase):
    """Riconciliazione: dichiarato nei manifest vs montato davvero nel gateway."""

    def test_declared_but_not_mounted_is_a_gap(self) -> None:
        decls = {"contabilita": {"mcp_servers": {"contabilita": {}, "normattiva": {}},
                                 "requires": {}, "datastores": [], "rag_collections": []}}
        d = pack_ops.drift(decls, mounted=["contabilita", "github"])
        self.assertEqual(d["missing"], [{"plugin": "contabilita", "server": "normattiva"}])
        self.assertEqual(d["mounted"], ["contabilita", "github"])
        self.assertNotIn("unavailable", d)

    def test_drift_unavailable_when_gateway_silent(self) -> None:
        """Gateway muto ≠ nessun drift: la differenza deve restare leggibile."""
        decls = {"contabilita": {"mcp_servers": {"normattiva": {}}}}
        d = pack_ops.drift(decls, mounted=None)
        self.assertTrue(d["unavailable"])
        self.assertNotIn("missing", d)

    def test_mounted_backend_not_declared_is_unmanaged(self) -> None:
        """Un backend montato che nessun plugin dichiara: residuo di una
        rimozione o aggiunta a mano. Non è un errore, ma va visto."""
        decls = {"contabilita": {"mcp_servers": {"contabilita": {}}}}
        d = pack_ops.drift(decls, mounted=["contabilita", "sedia"])
        self.assertEqual(d["unmanaged"], ["sedia"])
        self.assertEqual(d["missing"], [])

    def test_boot_report_makes_no_gateway_call(self) -> None:
        """Il path di avvio resta senza I/O: il drift è una funzione a parte."""
        decls = {"contabilita": {"mcp_servers": {"normattiva": {}}}}
        old = pack_mcp_mount.gateway_pdp.gw_tool
        try:
            def _boom(*_a, **_k):
                raise AssertionError("pending_report non deve chiamare il gateway")
            pack_mcp_mount.gateway_pdp.gw_tool = _boom
            report = pack_ops.pending_report(decls)
        finally:
            pack_mcp_mount.gateway_pdp.gw_tool = old
        self.assertEqual(report["plugins"], ["contabilita"])
        self.assertNotIn("drift", report)

    def test_report_endpoint_joins_declarations_and_gateway(self) -> None:
        old_tool = pack_mcp_mount.gateway_pdp.gw_tool
        old_authz = packs.gateway_pdp.require_authz
        old_decls = pack_ops.declarations
        try:
            packs.gateway_pdp.require_authz = lambda *a, **k: "davide"
            pack_ops.declarations = lambda: {
                "contabilita": {"mcp_servers": {"normattiva": {}}}}
            pack_mcp_mount.gateway_pdp.gw_tool = lambda *_a, **_k: (
                200, {"result": {"mcp_backends": [{"name": "github"}]}})
            report = asyncio.run(packs.pack_ops_report(mock.Mock(headers={}, client=None)))
        finally:
            pack_mcp_mount.gateway_pdp.gw_tool = old_tool
            packs.gateway_pdp.require_authz = old_authz
            pack_ops.declarations = old_decls

        self.assertEqual(report["drift"]["missing"],
                         [{"plugin": "contabilita", "server": "normattiva"}])
        self.assertEqual(report["drift"]["unmanaged"], ["github"])


class MountedBackendsTest(unittest.TestCase):
    """Lettura dei backend montati: `runtime.mcp_servers` è la sola fonte."""

    def test_reads_names_from_runtime_verb(self) -> None:
        old = pack_mcp_mount.gateway_pdp.gw_tool
        try:
            pack_mcp_mount.gateway_pdp.gw_tool = lambda *_a, **_k: (200, {"result": {
                "native": ["fs", "topic"],
                "mcp_backends": [{"name": "github"}, {"name": "contabilita"}],
                "count": 2}})
            self.assertEqual(pack_mcp_mount.mounted_backends("platform"),
                             ["contabilita", "github"])
        finally:
            pack_mcp_mount.gateway_pdp.gw_tool = old

    def test_gateway_error_is_none_not_empty(self) -> None:
        """`[]` direbbe «niente è montato» e farebbe sembrare tutto in drift."""
        old = pack_mcp_mount.gateway_pdp.gw_tool
        try:
            pack_mcp_mount.gateway_pdp.gw_tool = lambda *_a, **_k: (503, {})
            self.assertIsNone(pack_mcp_mount.mounted_backends("platform"))
        finally:
            pack_mcp_mount.gateway_pdp.gw_tool = old


if __name__ == "__main__":
    unittest.main()
