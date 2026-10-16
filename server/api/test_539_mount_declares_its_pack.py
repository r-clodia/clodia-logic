"""Un pack che monta un server MCP dice di esserne il proprietario (#539 §4).

La proprietà di un backend non si dichiara nel `pack.yaml` — quel file sta su un
volume che il gateway non possiede — si PROVA con il mount: `mcp.add` scrive
`pack:` nella voce, e da lì in poi quel pack può usare quel namespace per i
propri connettori (`backend_owners`, review di #354).

L'agent-server non lo passava mai. Due conseguenze: il backend di un pack
risultava «di nessuno» — cioè riservato a tutti e rivendicabile dal primo che lo
rimonta — e il pack non poteva provare l'appartenenza del proprio namespace.
Va insieme alla guardia del gateway (clodia-tools 2.90.0), e DOPO di essa: prima
della guardia, cominciare a timbrare i mount avrebbe trasferito proprietà invece
di fissarle.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import yaml

from . import gateway_pdp, pack_mcp_mount, plugin_import


class _Chiamate:
    def __init__(self):
        self.lista = []

    def __call__(self, tool, args, principal):
        self.lista.append((tool, args, principal))
        return 200, {"result": {"registered": ["normattiva"]}}


class _Manifests:
    """I `plugin.yaml` dei plugin, senza toccare il filesystem."""

    def __init__(self, tmp):
        self.tmp = tmp

    def scrivi(self, plugin: str, servers: dict) -> None:
        d = self.tmp / plugin
        d.mkdir(parents=True, exist_ok=True)
        (d / "plugin.yaml").write_text(
            yaml.safe_dump({"name": plugin, "mcp_servers": servers}), encoding="utf-8")


class MountDeclaresItsPackTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.meta = Path(tmp.name)
        p = patch.object(plugin_import, "PLUGINS_META_DIR", self.meta)
        p.start()
        self.addCleanup(p.stop)
        self.man = _Manifests(self.meta)
        self.gw = _Chiamate()
        q = patch.object(gateway_pdp, "gw_tool", self.gw)
        q.start()
        self.addCleanup(q.stop)

    def test_a_packs_plugin_mounts_in_the_name_of_its_pack(self) -> None:
        self.man.scrivi("studio-legale", {"normattiva": {"command": "python3", "args": []}})
        pack_mcp_mount.auto_mount_imported_mcp(
            {"kind": "pack", "pack": "legal-pack",
             "plugins": [{"plugin": "studio-legale"}]},
            "davide", trusted=True)
        self.assertEqual("legal-pack", self.gw.lista[0][1]["pack"])

    def test_the_wrapper_form_of_a_pack_is_covered_too(self) -> None:
        """Un pack importato come plugin wrapper porta `pack` e `plugin` nello
        stesso nodo: è la forma che `import_pack_zip` produce più spesso."""
        self.man.scrivi("legal-pack", {"normattiva": {"command": "python3", "args": []}})
        pack_mcp_mount.auto_mount_imported_mcp(
            {"kind": "pack", "pack": "legal-pack", "plugin": "legal-pack"},
            "davide", trusted=True)
        self.assertEqual("legal-pack", self.gw.lista[0][1]["pack"])

    def test_a_loose_plugin_claims_nothing(self) -> None:
        """Un plugin sciolto non è un pack: il backend resta di nessuno, che è
        la condizione che lo tiene riservato per tutti. Mandare un `pack` finto
        sarebbe una proprietà inventata."""
        self.man.scrivi("sciolto", {"normattiva": {"command": "python3", "args": []}})
        pack_mcp_mount.auto_mount_imported_mcp(
            {"plugin": "sciolto"}, "davide", trusted=True)
        self.assertNotIn("pack", self.gw.lista[0][1])

    def test_each_plugin_is_mounted_in_the_name_of_its_own_pack(self) -> None:
        """Il caso che un elenco di soli nomi non può distinguere: due pack in
        un solo import. Il secondo non deve ereditare il primo."""
        self.man.scrivi("uno", {"a": {"command": "python3", "args": []}})
        self.man.scrivi("due", {"b": {"command": "python3", "args": []}})
        pack_mcp_mount.auto_mount_imported_mcp(
            {"packs": [{"pack": "pack-uno", "plugins": [{"plugin": "uno"}]},
                       {"pack": "pack-due", "plugins": [{"plugin": "due"}]}]},
            "davide", trusted=True)
        self.assertEqual(["pack-uno", "pack-due"],
                         [c[1]["pack"] for c in self.gw.lista])

    def test_an_untrusted_source_still_mounts_nothing(self) -> None:
        """La Prima Legge viene prima della proprietà: un import da fonte
        esterna non avvia processi, col pack o senza."""
        self.man.scrivi("studio-legale", {"normattiva": {"command": "python3", "args": []}})
        out = pack_mcp_mount.auto_mount_imported_mcp(
            {"kind": "pack", "pack": "legal-pack",
             "plugins": [{"plugin": "studio-legale"}]}, "davide")
        self.assertEqual([], self.gw.lista)
        self.assertEqual(["normattiva"], out["mcp_mount"]["pending"])


class PluginOwnersTests(unittest.TestCase):
    def test_the_names_are_the_same_as_before(self) -> None:
        """`_plugin_names` resta la vista di prima: ordine e deduplica
        invariati, perché altri chiamanti la usano così."""
        albero = {"packs": [{"pack": "p1", "plugin": "p1", "plugins": [{"plugin": "a"}]},
                            {"pack": "p2", "plugins": [{"plugin": "a"},
                                                       {"plugin": "b"}]}]}
        self.assertEqual(["p1", "a", "b"], pack_mcp_mount._plugin_names(albero))

    def test_the_owner_of_a_duplicate_is_the_first_that_carried_it(self) -> None:
        albero = {"packs": [{"pack": "p1", "plugins": [{"plugin": "a"}]},
                            {"pack": "p2", "plugins": [{"plugin": "a"}]}]}
        self.assertEqual([("a", "p1")], pack_mcp_mount._plugin_owners(albero))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class ARefusedMountIsReportedNotRaisedTests(MountDeclaresItsPackTests):
    """clodia-platform#555: the gateway refused the `github` mount (409) with a
    plain-text body and the pack update died with HTTP 500 on `data.get`."""

    def setUp(self) -> None:
        super().setUp()
        rifiuto = patch.object(gateway_pdp, "gw_tool",
                               lambda tool, args, principal:
                               (409, "il backend MCP 'github' è già montato da un amministratore"))
        rifiuto.start()
        self.addCleanup(rifiuto.stop)

    def _monta(self):
        self.man.scrivi("it-pack", {"github": {"url": "https://api.githubcopilot.com/mcp/"}})
        return pack_mcp_mount.auto_mount_imported_mcp(
            {"kind": "pack", "pack": "it-pack", "plugins": [{"plugin": "it-pack"}]},
            "davide", trusted=True)

    def test_the_refusal_is_in_the_result(self) -> None:
        out = self._monta()
        testo = repr(out)
        self.assertIn("409", testo)
        self.assertIn("già montato", testo)

    # The inherited tests assert a successful mount: not applicable here.
    test_a_packs_plugin_mounts_in_the_name_of_its_pack = None
    test_the_wrapper_form_of_a_pack_is_covered_too = None
    test_a_loose_plugin_claims_nothing = None
    test_each_plugin_is_mounted_in_the_name_of_its_own_pack = None
