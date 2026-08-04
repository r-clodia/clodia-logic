"""Dichiarazioni di flusso nel manifest di un pack (clodia-platform#104).

Un pack dichiara `egress:` (dove scrive) e `ingress:` (quali fonti non
contaminano). È una dichiarazione di FLUSSO, non di permessi, e l'installazione
NON la concede: la dichiarazione è dell'autore del pack, la concessione è
dell'owner. Un pack scaricato da un repo di terzi non deve poter decidere quali
fonti non contaminano il canale di chi lo installa — è l'unica delega che va
nella direzione d'errore silenziosa.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import pack_import


class DeclaredFlowsTests(unittest.TestCase):
    def test_it_reads_both_directions(self):
        f = pack_import.declared_flows(
            {"name": "p", "egress": ["mailto:a@b.it"], "ingress": ["mcp:normattiva."]})
        self.assertEqual(f, {"egress": ["mailto:a@b.it"],
                             "ingress": ["mcp:normattiva."]})

    def test_a_single_string_is_accepted(self):
        """Un autore che scrive `ingress: mcp:normattiva.` intende una voce: farlo
        fallire su una virgola insegna solo a non dichiarare niente."""
        self.assertEqual(pack_import.declared_flows({"ingress": "mcp:normattiva."}),
                         {"ingress": ["mcp:normattiva."]})

    def test_a_pack_without_declarations_declares_nothing(self):
        self.assertEqual(pack_import.declared_flows({"name": "p"}), {})
        self.assertEqual(pack_import.declared_flows({"egress": [], "ingress": None}), {})

    def test_it_does_not_grant(self):
        """`declared_flows` LEGGE. Nessuna chiamata al gateway, per costruzione:
        se leggere concedesse, l'atto dell'owner sarebbe l'installazione, cioè un
        atto in cui non ha visto le voci."""
        from . import gateway_admin
        with patch.object(gateway_admin, "flow_allow",
                          side_effect=AssertionError("non deve concedere")):
            pack_import.declared_flows({"ingress": ["mcp:normattiva."]})


class ValidationIsBestEffortTests(unittest.TestCase):
    def test_an_unreachable_gateway_does_not_break_the_install(self):
        """Un pack non deve diventare non installabile perché il gateway si stava
        riavviando: le dichiarazioni restano in sospeso, che è lo stato giusto."""
        from . import gateway_admin
        with patch.object(gateway_admin, "flow_allow",
                          side_effect=OSError("connection refused")):
            r = pack_import.validate_flows({"ingress": ["mcp:normattiva."]})
        self.assertEqual(r["granted"], [])
        self.assertIn("unavailable", r)

    def test_validation_asks_the_gateway_in_dry_run(self):
        """I criteri (schemi per direzione, voci degeneri) stanno nel gateway: una
        seconda copia qui divergerebbe, e divergerebbe in silenzio."""
        from . import gateway_admin
        seen = {}

        def fake(flows, source="", validate=False):
            seen.update({"flows": flows, "validate": validate, "source": source})
            return {"granted": [], "refused": []}

        with patch.object(gateway_admin, "flow_allow", fake):
            pack_import.validate_flows({"ingress": ["mcp:normattiva."]}, source="pack:x")
        self.assertTrue(seen["validate"], "la convalida non deve concedere")
        self.assertEqual(seen["source"], "pack:x")


class ApprovalBookkeepingTests(unittest.TestCase):
    """Si registra COSA è stato approvato, non solo che lo è.

    È il caso che conta davvero: un pack aggiornato dichiara una voce nuova. Con
    un flag booleano erediterebbe l'approvazione data a ciò che dichiarava prima —
    cioè un aggiornamento diventerebbe il modo di aggiungere una fonte fidata
    senza che nessuno la veda. Con l'elenco, la voce nuova risulta non approvata.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self._p = patch.object(pack_import, "PACKS_META_DIR", self.root)
        self._p.start()
        self.addCleanup(self._p.stop)

    def _write(self, declared):
        import yaml
        d = self.root / "p"
        d.mkdir(parents=True, exist_ok=True)
        (d / "pack.yaml").write_text(
            yaml.safe_dump({"name": "p", "flows": {"declared": declared,
                                                   "approved": False}}),
            encoding="utf-8")

    def _read(self):
        import yaml
        return yaml.safe_load((self.root / "p" / "pack.yaml").read_text())["flows"]

    def test_approving_everything_declared_marks_it_approved(self):
        from . import packs
        self._write({"ingress": ["mcp:normattiva."]})
        packs._set_pack_flows_approved("p", [{"uri": "mcp:normattiva."}])
        f = self._read()
        self.assertTrue(f["approved"])
        self.assertEqual(f["granted"], ["mcp:normattiva."])

    def test_approving_a_subset_does_not_mark_it_approved(self):
        from . import packs
        self._write({"ingress": ["mcp:normattiva."], "egress": ["mailto:a@b.it"]})
        packs._set_pack_flows_approved("p", [{"uri": "mcp:normattiva."}])
        self.assertFalse(self._read()["approved"])

    def test_a_new_declaration_after_an_update_is_not_approved(self):
        from . import packs
        self._write({"ingress": ["mcp:normattiva."]})
        packs._set_pack_flows_approved("p", [{"uri": "mcp:normattiva."}])
        self.assertTrue(self._read()["approved"])
        # il pack si aggiorna e dichiara una fonte in più
        import yaml
        path = self.root / "p" / "pack.yaml"
        m = yaml.safe_load(path.read_text())
        m["flows"]["declared"]["ingress"].append("mcp:altrove.")
        path.write_text(yaml.safe_dump(m), encoding="utf-8")
        packs._set_pack_flows_approved("p", [])
        f = self._read()
        self.assertFalse(f["approved"], "una voce nuova non è approvata")
        self.assertEqual(f["granted"], ["mcp:normattiva."])


if __name__ == "__main__":
    unittest.main()
