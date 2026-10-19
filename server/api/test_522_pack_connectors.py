"""Il blocco `connectors:` di un pack sopravvive all'installazione (#522).

clodia-platform#516 ha dato la grammatica dei connettori e #522 porta la prima
dichiarazione vera: Telegram, in comms-pack. Chi la legge non c'è ancora — il
registry che sostituisce le tabelle cablate del gateway è #518 — ma quando
arriverà leggerà il manifest REGISTRATO in `<datadir>/packs/<pack>/pack.yaml`,
non il sorgente, che dopo l'import può non esistere più.

E lì `install_pack_from_root` scriveva un manifest curato coi soli campi noti
(name/description/version/source/agents/plugins, più flows/upstream): un
`connectors:` dichiarato dall'autore spariva all'installazione. Il difetto non
dà errore — dà un elenco vuoto, cioè un registry che dice «nessun pack dichiara
connettori» mentre il pack lo dichiara eccome. È lo stesso difetto di #339 sui
doc del pack, su un campo che ancora non ha un lettore: va chiuso ORA, perché
quando il lettore ci sarà il sintomo sarà «il connettore non compare» e la
causa starà tre issue più indietro.

Qui si verifica che la dichiarazione arrivi, non che sia valida: la grammatica
sta nel gateway (`server/connectors/manifest.py` in clodia-tools) e il rifiuto a
install time è #519. Una seconda copia dei criteri da questa parte del confine
divergerebbe, e divergerebbe in silenzio.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from ..config import workspace_path
from . import catalog, pack_import, plugin_import

#: La dichiarazione vera, quella di comms-pack.
COMMS_PACK = Path(workspace_path("catalogs/packs/comms-pack/pack.yaml"))


def _plugin(dir_: Path, nome: str) -> None:
    pdir = dir_ / "plugins" / nome
    d = pdir / ".claude-plugin"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(
        json.dumps({"name": nome, "description": "plugin di prova",
                    "version": "1.0.0"}),
        encoding="utf-8")
    skill = pdir / "skills" / "dummy"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\nname: dummy\ndescription: skill di prova\n---\n# dummy\n",
        encoding="utf-8")


#: Un connettore minimo ma completo: schema, provider, verbo, azione di topic.
#: Serve a provare la RICOPIATURA, non la validità — per quella c'è il gateway.
CONNETTORE = {
    "id": "citofono",
    "manifest_version": 1,
    "namespace": "citofono",
    "label": "Citofono",
    "schemes": [{"scheme": "cit", "direction": "both",
                 "forms": [{"label": "interno", "pattern": r"^\d+$"}]}],
    "providers": [{"id": "sip", "label": "SIP"}],
    "verbs": [{"verb": "citofono.chiama", "direction": "egress",
               "scheme": "cit", "arg": "interno"}],
    "topic_actions": [{"id": "citofono-link", "label": "Collega un interno",
                       "fields": [{"name": "interno"}],
                       "adds": {"egress": ["cit:{interno}"]}}],
}


class DeclaredConnectorsTests(unittest.TestCase):
    def test_it_reads_the_block(self):
        self.assertEqual(
            pack_import.declared_connectors({"name": "p",
                                             "connectors": [CONNETTORE]}),
            [CONNETTORE])

    def test_a_pack_without_connectors_declares_none(self):
        self.assertEqual(pack_import.declared_connectors({"name": "p"}), [])
        self.assertEqual(pack_import.declared_connectors({"connectors": []}), [])
        self.assertEqual(pack_import.declared_connectors({"connectors": "cit"}), [])

    def test_it_does_not_grant(self):
        """Leggere non concede. Vale qui come per `declared_flows`: la
        dichiarazione è dell'autore del pack, il permesso è dell'owner, e un
        pack di terzi non deve poter decidere a che cosa arriva chi lo
        installa."""
        from . import gateway_admin
        with patch.object(gateway_admin, "flow_allow",
                          side_effect=AssertionError("non deve concedere")):
            pack_import.declared_connectors({"connectors": [CONNETTORE]})


class ConnectorsSurviveInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="packconn-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.packs_meta = self.root / "packs-meta"
        self.packs_meta.mkdir(parents=True)
        self._old_meta = pack_import.PACKS_META_DIR
        pack_import.PACKS_META_DIR = self.packs_meta
        self.addCleanup(setattr, pack_import, "PACKS_META_DIR", self._old_meta)

        # Come in test_pack_docs_copied: senza isolare anche plugin e skill,
        # l'import a cascata scriverebbe nei path DATA veri.
        self.plugins_meta = self.root / "plugins-meta"
        self.skills_meta = self.root / "skills-meta"
        self._old_plugins = plugin_import.PLUGINS_META_DIR
        plugin_import.PLUGINS_META_DIR = self.plugins_meta
        self.addCleanup(setattr, plugin_import, "PLUGINS_META_DIR", self._old_plugins)
        self._old_skills = catalog.DATA_SKILLS_DIR
        catalog.DATA_SKILLS_DIR = self.skills_meta
        self.addCleanup(setattr, catalog, "DATA_SKILLS_DIR", self._old_skills)
        # Since #519 a pack declaring `connectors:` is installed only after the
        # gateway validates the block (fail-closed): these tests are about the
        # declaration surviving the install, so the gateway answers "ok".
        from . import gateway_admin
        gw = patch.object(gateway_admin, "connectors_validate",
                          return_value={"ok": True, "errors": [], "ignored": []})
        gw.start()
        self.addCleanup(gw.stop)

    def _pack_root(self, nome: str, manifest: dict) -> Path:
        d = self.root / "src" / nome
        d.mkdir(parents=True)
        (d / "pack.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
            encoding="utf-8")
        _plugin(d, f"plugin-{nome}")
        return d

    def _installato(self, nome: str) -> dict:
        return yaml.safe_load(
            (self.packs_meta / nome / "pack.yaml").read_text(encoding="utf-8")) or {}

    def test_the_declaration_reaches_the_registered_manifest(self):
        d = self._pack_root("con-connettore",
                            {"name": "con-connettore", "version": "1.0.0",
                             "connectors": [CONNETTORE]})
        pack_import.install_pack_from_root(d, source="test")
        self.assertEqual(self._installato("con-connettore").get("connectors"),
                         [CONNETTORE])

    def test_nothing_of_it_is_lost_on_the_way(self):
        """Non «c'è un blocco connectors», ma «è LO STESSO blocco»: un giro di
        copia che perdesse le forme di uno schema o il template di un'azione
        darebbe un connettore che esiste e non funziona, cioè il caso peggiore
        da diagnosticare."""
        d = self._pack_root("integrale",
                            {"name": "integrale", "version": "1.0.0",
                             "connectors": [CONNETTORE]})
        pack_import.install_pack_from_root(d, source="test")
        c = self._installato("integrale")["connectors"][0]
        self.assertEqual(c["schemes"][0]["forms"][0]["pattern"], r"^\d+$")
        self.assertEqual(c["verbs"][0]["verb"], "citofono.chiama")
        self.assertEqual(c["topic_actions"][0]["adds"]["egress"], ["cit:{interno}"])

    def test_an_update_brings_the_new_declaration(self):
        """Un connettore aggiunto in una versione successiva del pack arriva con
        l'Update: se no la dichiarazione varrebbe solo per chi installa da zero,
        e sulle istanze vive non comparirebbe mai."""
        d = self._pack_root("aggiornato", {"name": "aggiornato", "version": "1.0.0"})
        pack_import.install_pack_from_root(d, source="test")
        self.assertIsNone(self._installato("aggiornato").get("connectors"))
        (d / "pack.yaml").write_text(
            yaml.safe_dump({"name": "aggiornato", "version": "1.1.0",
                            "connectors": [CONNETTORE]},
                           sort_keys=False, allow_unicode=True),
            encoding="utf-8")
        pack_import.install_pack_from_root(d, source="test", force=True)
        self.assertEqual(self._installato("aggiornato")["connectors"], [CONNETTORE])

    def test_a_pack_without_connectors_has_no_empty_key(self):
        """Nessun campo vuoto nei manifest di tutti gli altri pack: un
        `connectors: []` registrato ovunque è rumore che poi qualcuno
        interpreta."""
        d = self._pack_root("senza", {"name": "senza", "version": "1.0.0"})
        pack_import.install_pack_from_root(d, source="test")
        self.assertNotIn("connectors", self._installato("senza"))


class CommsPackDeclaresTelegramTests(unittest.TestCase):
    """La dichiarazione vera di #522, letta dal pack che la porta."""

    def setUp(self) -> None:
        self.manifest = yaml.safe_load(COMMS_PACK.read_text(encoding="utf-8")) or {}
        self.telegram = next(
            (c for c in pack_import.declared_connectors(self.manifest)
             if c.get("id") == "telegram"), None)
        self.assertIsNotNone(self.telegram, "comms-pack non dichiara Telegram")

    def test_both_forms_of_tg_and_no_third(self):
        """clodia-platform#363: un gruppo è un id numerico, una persona è un
        @handle, e non c'è una terza forma. Sono gli stessi due pattern di
        `egress._TG_GROUP` / `_TG_HANDLE`: la parità con le tabelle di oggi è
        ciò che rende la dichiarazione sostituibile a quelle (#518)."""
        schemi = self.telegram["schemes"]
        self.assertEqual([s["scheme"] for s in schemi], ["tg"])
        self.assertEqual(schemi[0]["direction"], "both")
        self.assertEqual([f["pattern"] for f in schemi[0]["forms"]],
                         [r"^-?\d+$", r"^@[A-Za-z0-9_]{5,32}$"])

    def test_the_link_action_writes_both_directions_and_binds(self):
        """Il bottone Telegram (`connectors.actions.apply` del gateway) fa i DUE
        passi insieme: whitelist nelle due direzioni E binding del canale. Senza il secondo il messaggero non
        riporta nulla pur con la whitelist scritta — era il sintomo del 23 set
        2026, e una dichiarazione che lo dimenticasse lo farebbe tornare."""
        a = self.telegram["topic_actions"][0]
        self.assertEqual(a["binding"], "channel")
        self.assertEqual(a["adds"]["ingress"], ["tg:{chat_id}"])
        self.assertEqual(a["adds"]["egress"], ["tg:{chat_id}"])

    def test_it_declares_only_the_two_verbs_the_pdp_checks_today(self):
        """Parità, in negativo: `egress._SPECS` controlla `telegram.send` e
        `telegram.send_file` e basta. Dichiarare anche `telegram.inbox` come
        `source` sembrerebbe più completo e sarebbe un controllo che oggi non
        esiste — cioè una differenza di comportamento introdotta di nascosto in
        una migrazione che deve essere a parità."""
        self.assertEqual([v["verb"] for v in self.telegram["verbs"]],
                         ["telegram.send", "telegram.send_file"])
        self.assertTrue(all(v["direction"] == "egress"
                            for v in self.telegram["verbs"]))

    def test_the_connector_id_is_the_one_the_relay_binds_on(self):
        """Un solo nome per la stessa cosa: l'id del connettore è la chiave dei
        binding (`channel-bindings.json`, #521) ed è ciò che l'adapter del relay
        dichiara. Se divergono, i binding di un canale e la sua dichiarazione
        parlano di due connettori diversi e nessuno se ne accorge."""
        from .telegram_relay_adapter import ADAPTER
        self.assertEqual(self.telegram["id"], ADAPTER.connector)
        self.assertEqual(self.telegram["schemes"][0]["scheme"], ADAPTER.uri_scheme)


if __name__ == "__main__":
    unittest.main()
