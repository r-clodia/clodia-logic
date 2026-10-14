"""Il mount di un pack dichiara il PACK che lo monta (clodia-platform#539 §4).

Seguito di #354: il gateway decide chi possiede un backend MCP da CHI l'ha
montato (`register_mcp_core(..., pack=…)` scrive `pack:` nella voce), e da quella
proprietà dipende il diritto del pack a usare quel namespace per i propri
connettori. Da questo lato il `pack` non veniva passato: il backend restava «di
nessuno» e il connettore del pack finiva rifiutato come se reclamasse il
namespace di un altro.

Va DOPO la guardia del gateway (clodia-tools, §4): prima di quella, iniziare a
timbrare i mount trasferirebbe davvero le proprietà che la issue segnala come
inattive.
"""
from __future__ import annotations

import unittest

import yaml

from . import pack_mcp_mount, plugin_import


class _Gateway:
    """`gw_tool` finto: registra le chiamate e risponde «montato»."""

    def __init__(self):
        self.chiamate: list[tuple[str, dict, str]] = []

    def __call__(self, tool, args, principal):
        self.chiamate.append((tool, args, principal))
        servers = list(((args.get("config") or {}).get("mcpServers") or {}))
        return 200, {"result": {"registered": servers}}


class _Plugins:
    """Manifest di plugin su disco, nella meta dir vera ma temporanea."""

    def __init__(self, testcase, **plugins):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for nome, servers in plugins.items():
            d = self.root / nome
            d.mkdir()
            (d / "plugin.yaml").write_text(
                yaml.safe_dump({"name": nome, "mcp_servers": servers}),
                encoding="utf-8")


class PackOwnershipTests(unittest.TestCase):
    SERVER = {"normattiva": {"command": "python3", "args": ["x.py"]}}

    def setUp(self):
        from unittest.mock import patch
        self.plugins = _Plugins(self, **{"studio-legale": self.SERVER})
        p = patch.object(plugin_import, "PLUGINS_META_DIR", self.plugins.root)
        p.start()
        self.addCleanup(p.stop)
        self.gw = _Gateway()
        g = patch.object(pack_mcp_mount.gateway_pdp, "gw_tool", self.gw)
        g.start()
        self.addCleanup(g.stop)

    def _monta(self, result):
        pack_mcp_mount.auto_mount_imported_mcp(result, "davide", trusted=True)
        return self.gw.chiamate

    def test_il_pack_contenitore_arriva_al_gateway(self):
        chiamate = self._monta({"kind": "pack", "pack": "studio-pack",
                                "plugins": [{"plugin": "studio-legale"}]})
        self.assertEqual(len(chiamate), 1)
        tool, args, principal = chiamate[0]
        self.assertEqual((tool, principal), ("mcp.add", "davide"))
        self.assertEqual(args["pack"], "studio-pack")
        self.assertIn("normattiva", args["config"]["mcpServers"])

    def test_vale_anche_per_i_pack_annidati(self):
        """`kind: packs` — una directory di pack: ogni plugin prende il pack
        che lo CONTIENE, non il primo della lista."""
        self.plugins = _Plugins(self, **{"studio-legale": self.SERVER,
                                         "contabile": {"fatture": {"command": "x"}}})
        from unittest.mock import patch
        p = patch.object(plugin_import, "PLUGINS_META_DIR", self.plugins.root)
        p.start()
        self.addCleanup(p.stop)
        chiamate = self._monta({"kind": "packs", "packs": [
            {"pack": "studio-pack", "plugins": [{"plugin": "studio-legale"}]},
            {"pack": "contabile-pack", "plugins": [{"plugin": "contabile"}]},
        ]})
        self.assertEqual([(c[1]["pack"], sorted(c[1]["config"]["mcpServers"]))
                          for c in chiamate],
                         [("studio-pack", ["normattiva"]),
                          ("contabile-pack", ["fatture"])])

    def test_il_wrapper_di_un_plugin_nudo_e_comunque_un_pack(self):
        chiamate = self._monta({"kind": "pack", "pack": "studio-legale",
                                "plugins": [{"plugin": "studio-legale"}],
                                "wrapped": True})
        self.assertEqual(chiamate[0][1]["pack"], "studio-legale")

    def test_senza_contenitore_non_si_inventa_un_proprietario(self):
        """Un risultato senza pack (forma storica) monta come prima: meglio un
        backend di nessuno che uno timbrato con un nome indovinato — il timbro
        è ciò che concede un namespace."""
        chiamate = self._monta({"plugin": "studio-legale"})
        self.assertNotIn("pack", chiamate[0][1])

    def test_niente_mount_da_fonte_non_fidata(self):
        """La Prima Legge viene prima della proprietà: `trusted=False` non
        chiama il gateway, con o senza pack."""
        result = {"kind": "pack", "pack": "studio-pack",
                  "plugins": [{"plugin": "studio-legale"}]}
        pack_mcp_mount.auto_mount_imported_mcp(result, "davide")
        self.assertEqual(self.gw.chiamate, [])
        self.assertEqual(result["mcp_mount"]["pending"], ["normattiva"])


class PluginEntriesTests(unittest.TestCase):
    def test_un_plugin_compare_una_volta_sola(self):
        entries = pack_mcp_mount._plugin_entries({
            "pack": "studio-pack",
            "plugins": [{"plugin": "studio-legale"}, {"plugin": "studio-legale"}]})
        self.assertEqual(entries, [("studio-legale", "studio-pack")])

    def test_i_nomi_restano_disponibili_da_soli(self):
        """`packs.py` chiede i soli nomi: l'helper storico non sparisce."""
        self.assertEqual(
            pack_mcp_mount._plugin_names({"pack": "p", "plugins": [{"plugin": "a"}]}),
            ["a"])


if __name__ == "__main__":
    unittest.main()
