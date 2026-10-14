"""clodia-platform#519 — il blocco `connectors:` si convalida prima di installare.

#522 fa arrivare la dichiarazione fino al manifest registrato; qui si decide se
quella dichiarazione è installabile su questa istanza. La domanda la fa il
gateway, perché la grammatica, il registry e le liste vivono là: una seconda
copia dei criteri da questa parte del confine divergerebbe, e divergerebbe
proprio dalla parte in cui gira il codice degli agenti.

Due esiti che NON sono simmetrici, ed è il punto di questo file:

* **verdetto negativo** — il manifest è sbagliato: l'installazione non avviene,
  e non avviene *prima* di toccare qualunque cosa. Un rifiuto che lascia dietro
  di sé mezzo pack installato è un rifiuto a metà;
* **gateway irraggiungibile** — l'installazione procede con un avviso. Un pack
  non deve diventare non installabile perché il gateway si stava riavviando, ed
  è sicuro perché la convalida non è ciò che protegge l'istanza: il registry
  rifiuta da solo un manifest malformato, quindi un blocco mai convalidato
  resta inerte. Il gemello di questo test sta nel gateway
  (`test_a_refused_block_is_inert_even_if_it_was_never_validated`).
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from . import catalog, gateway_admin, pack_import, plugin_import

#: Il connettore di prova di #522: qui non conta che sia valido — a dirlo è il
#: gateway, che in questi test è finto — conta cosa succede alla risposta.
CONNETTORE = {
    "id": "citofono",
    "manifest_version": 1,
    "namespace": "citofono",
    "label": "Citofono",
    "schemes": [{"scheme": "cit", "direction": "both"}],
    "verbs": [{"verb": "citofono.chiama", "direction": "egress",
               "scheme": "cit", "arg": "interno"}],
}


def _plugin(dir_: Path, nome: str) -> None:
    pdir = dir_ / "plugins" / nome
    d = pdir / ".claude-plugin"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(
        json.dumps({"name": nome, "description": "plugin di prova",
                    "version": "1.0.0"}), encoding="utf-8")
    skill = pdir / "skills" / "dummy"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\nname: dummy\ndescription: skill di prova\n---\n# dummy\n",
        encoding="utf-8")


class _Install(unittest.TestCase):
    """Un'installazione vera, con tutti i path su una directory temporanea."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="conn519-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.packs_meta = self.root / "packs-meta"
        self.packs_meta.mkdir(parents=True)
        for mod, attr, value in (
            (pack_import, "PACKS_META_DIR", self.packs_meta),
            (plugin_import, "PLUGINS_META_DIR", self.root / "plugins-meta"),
            (catalog, "DATA_SKILLS_DIR", self.root / "skills-meta"),
        ):
            old = getattr(mod, attr)
            setattr(mod, attr, value)
            self.addCleanup(setattr, mod, attr, old)

    def _pack_root(self, nome: str, *, connectors=(CONNETTORE,)) -> Path:
        d = self.root / "src" / nome
        d.mkdir(parents=True)
        manifest = {"name": nome, "version": "1.0.0"}
        if connectors:
            manifest["connectors"] = list(connectors)
        (d / "pack.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
            encoding="utf-8")
        _plugin(d, f"plugin-{nome}")
        return d

    def _installato(self, nome: str) -> dict:
        path = self.packs_meta / nome / "pack.yaml"
        if not path.is_file():
            return {}
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


class VerdictTests(_Install):
    def test_a_clean_verdict_installs_and_keeps_the_declaration(self) -> None:
        with patch.object(gateway_admin, "connectors_validate",
                          return_value={"ok": True, "errors": [], "ignored": [],
                                        "connectors": [{"id": "citofono"}]}):
            out = pack_import.install_pack_from_root(self._pack_root("p1"),
                                                     source="test")
        self.assertEqual(self._installato("p1").get("connectors"), [CONNETTORE])
        self.assertTrue(out["connectors"]["checked"])
        self.assertEqual(out["connectors"]["declared"], ["citofono"])

    def test_a_negative_verdict_refuses_the_install(self) -> None:
        with patch.object(gateway_admin, "connectors_validate",
                          return_value={"ok": False,
                                        "errors": ["scheme 'cit' già dichiarato"],
                                        "ignored": [], "connectors": []}):
            with self.assertRaises(pack_import.PackImportError) as e:
                pack_import.install_pack_from_root(self._pack_root("p2"),
                                                   source="test")
        self.assertIn("cit", str(e.exception))

    def test_a_refused_pack_leaves_nothing_behind(self) -> None:
        """Il rifiuto arriva PRIMA di installare seed e plugin: metà pack
        installato è peggio di un pack non installato, perché nessuno lo cerca."""
        with patch.object(gateway_admin, "connectors_validate",
                          return_value={"ok": False, "errors": ["no"],
                                        "ignored": [], "connectors": []}):
            with self.assertRaises(pack_import.PackImportError):
                pack_import.install_pack_from_root(self._pack_root("p3"),
                                                   source="test")
        self.assertEqual(self._installato("p3"), {})
        self.assertFalse((self.root / "plugins-meta" / "plugin-p3").exists())

    def test_an_inert_connector_does_not_stop_the_install(self) -> None:
        """Il caso di comms-pack: dichiarazione corretta, ma il gateway
        implementa ancora da sé quel connettore. Si installa, e si scrive perché
        non ha effetto — se no il sintomo sarà «il connettore non compare» e la
        causa starà nello stato di una migrazione che nessuno ricorda."""
        motivo = ("pack 'p4', connector 'citofono': namespace 'citofono' is "
                  "still implemented by this gateway")
        with patch.object(gateway_admin, "connectors_validate",
                          return_value={"ok": True, "errors": [],
                                        "ignored": [motivo], "connectors": []}):
            out = pack_import.install_pack_from_root(self._pack_root("p4"),
                                                     source="test")
        self.assertEqual(self._installato("p4").get("connectors"), [CONNETTORE])
        self.assertEqual(self._installato("p4").get("connectors_inactive"),
                         [motivo])
        self.assertEqual(out["connectors"]["ignored"], [motivo])

    def test_an_unreachable_gateway_does_not_block_the_install(self) -> None:
        with patch.object(gateway_admin, "connectors_validate",
                          side_effect=ConnectionError("gateway giù")):
            out = pack_import.install_pack_from_root(self._pack_root("p5"),
                                                     source="test")
        self.assertEqual(self._installato("p5").get("connectors"), [CONNETTORE])
        self.assertFalse(out["connectors"]["checked"])
        self.assertIn("gateway giù", out["connectors"]["unavailable"])

    def test_a_pack_without_connectors_asks_nothing(self) -> None:
        """La stragrande maggioranza dei pack non dichiara connettori: non deve
        pagare una chiamata HTTP, e soprattutto non deve diventare
        non installabile se quella chiamata un giorno cambia."""
        with patch.object(gateway_admin, "connectors_validate",
                          side_effect=AssertionError("non deve chiedere")):
            pack_import.install_pack_from_root(
                self._pack_root("p6", connectors=()), source="test")
        self.assertEqual(self._installato("p6").get("connectors"), None)

    def test_the_question_carries_the_pack_and_the_block_as_declared(self) -> None:
        visto = {}

        def _spia(pack, connectors):
            visto["pack"], visto["connectors"] = pack, connectors
            return {"ok": True, "errors": [], "ignored": [], "connectors": []}

        with patch.object(gateway_admin, "connectors_validate", _spia):
            pack_import.install_pack_from_root(self._pack_root("p7"),
                                               source="test")
        self.assertEqual(visto["pack"], "p7")
        self.assertEqual(visto["connectors"], [CONNETTORE])


class ClientTests(unittest.TestCase):
    """Il client parla col gateway, non con l'API degli agent."""

    def test_the_url_is_the_gateway_root(self) -> None:
        with patch.dict("os.environ",
                        {"CLODIA_TOOLS_MCP_URL": "http://gw:7849/mcp/"}):
            self.assertEqual(gateway_admin._root_url(), "http://gw:7849")
            self.assertEqual(gateway_admin._base_url(),
                             "http://gw:7849/internal/agents")

    def test_it_posts_the_declaration_and_returns_the_verdict(self) -> None:
        class _R:
            status_code = 200

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"ok": True, "errors": [], "ignored": []}

        visto = {}

        def _post(url, headers=None, json=None, timeout=None):
            visto["url"], visto["json"] = url, json
            return _R()

        with patch.object(gateway_admin.requests, "post", _post), \
                patch.object(gateway_admin, "_headers", lambda: {}):
            out = gateway_admin.connectors_validate("comms-pack", [CONNETTORE])
        self.assertTrue(out["ok"])
        self.assertTrue(visto["url"].endswith("/internal/connectors/validate"))
        self.assertEqual(visto["json"],
                         {"pack": "comms-pack", "connectors": [CONNETTORE]})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
