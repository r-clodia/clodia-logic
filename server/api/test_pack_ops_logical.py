"""Setup di un pack come funzione, non come turno di un agente.

`pack_ops.trigger_reconcile` consegna la riconciliazione a un LLM che decide
da sé quali tool chiamare, leggendo un prompt. `run_logical_setup` fa lo
stesso lavoro meccanico (pip/npm/bin/rag_collections) iterando su dati
strutturati e chiamando i verbi direttamente — nato dal bottone "Aggiorna
tutto" (18 set 2026, richiesta di Davide: «il setup potrebbe essere logico e
non agentico»).
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import pack_ops_logical as pol


def _gw(risposte: dict):
    """`gateway_pdp.gw_tool` finto: `risposte[verb]` è `(status, data)` o
    una funzione di `arguments -> (status, data)` per casi che dipendono
    dall'argomento (più pacchetti dello stesso verbo, esiti diversi)."""
    def _call(verb, arguments, principal):
        r = risposte.get(verb)
        if callable(r):
            return r(arguments)
        return r or (500, {"error": f"verbo non atteso nel test: {verb}"})
    return _call


class PipNpmTests(unittest.TestCase):
    def test_a_successful_install_is_recorded_as_done(self):
        with patch.object(pol.gateway_pdp, "gw_tool",
                          side_effect=_gw({"packs.install_pip":
                                          (200, {"result": {"ok": True}})})):
            out = pol.run_logical_setup(
                "demo", {"requires": {"pip": ["mcp"]}}, "davide")
        self.assertEqual(out["done"], ["pip:mcp"])
        self.assertEqual(out["gaps"], [])

    def test_a_failed_install_is_a_gap_not_an_exception(self):
        with patch.object(pol.gateway_pdp, "gw_tool",
                          side_effect=_gw({"packs.install_npm":
                                          (500, {"result": {"ok": False,
                                                            "stderr_tail": "404 not found"}})})):
            out = pol.run_logical_setup(
                "demo", {"requires": {"npm": ["pacchetto-inesistente"]}}, "davide")
        self.assertEqual(out["done"], [])
        self.assertEqual(len(out["gaps"]), 1)
        self.assertEqual(out["gaps"][0]["kind"], "npm")
        self.assertIn("404", out["gaps"][0]["detail"])

    def test_a_gateway_call_raising_is_a_gap_not_a_crash(self):
        def _boom(*a, **k):
            raise ConnectionError("gateway irraggiungibile")
        with patch.object(pol.gateway_pdp, "gw_tool", side_effect=_boom):
            out = pol.run_logical_setup(
                "demo", {"requires": {"pip": ["mcp"]}}, "davide")
        self.assertEqual(out["done"], [])
        self.assertEqual(out["gaps"][0]["kind"], "pip")

    def test_multiple_packages_are_each_their_own_call(self):
        chiamate = []
        def _pip(arguments):
            chiamate.append(arguments["packages"][0])
            return (200, {"result": {"ok": True}})
        with patch.object(pol.gateway_pdp, "gw_tool",
                          side_effect=_gw({"packs.install_pip": _pip})):
            out = pol.run_logical_setup(
                "demo", {"requires": {"pip": ["a", "b"]}}, "davide")
        self.assertEqual(chiamate, ["a", "b"])
        self.assertEqual(out["done"], ["pip:a", "pip:b"])


class BinChecksTests(unittest.TestCase):
    def test_a_found_binary_is_done_not_installed(self):
        """check_command è READ-ONLY: un binario mancante non si installa da
        qui, si segnala soltanto."""
        with patch.object(pol.gateway_pdp, "gw_tool",
                          side_effect=_gw({"packs.check_command":
                                          (200, {"result": {"found": True}})})):
            out = pol.run_logical_setup("demo", {"requires": {"bin": ["node"]}}, "davide")
        self.assertEqual(out["done"], ["bin:node"])

    def test_a_missing_binary_is_a_gap_with_a_reason_that_says_why(self):
        with patch.object(pol.gateway_pdp, "gw_tool",
                          side_effect=_gw({"packs.check_command":
                                          (200, {"result": {"found": False}})})):
            out = pol.run_logical_setup("demo", {"requires": {"bin": ["ffmpeg"]}}, "davide")
        self.assertEqual(out["gaps"][0]["kind"], "bin")
        self.assertIn("immagine", out["gaps"][0]["detail"])


class RagCollectionTests(unittest.TestCase):
    def test_a_new_collection_is_created_and_recorded_done(self):
        with patch.object(pol.gateway_pdp, "gw_tool",
                          side_effect=_gw({"rag.create_collection": (200, {"result": {}})})):
            out = pol.run_logical_setup(
                "demo", {"rag_collections": [{"name": "eu-normativa", "tier": "SEAL-2"}]},
                "davide")
        self.assertIn("rag_collection:eu-normativa", out["done"])

    def test_an_already_existing_collection_is_not_a_gap(self):
        """Idempotente: ricreare una collection che c'è già non deve
        risultare in un gap che non se ne va mai."""
        with patch.object(pol.gateway_pdp, "gw_tool",
                          side_effect=_gw({"rag.create_collection":
                                          (400, {"error": "collection già esistente"})})):
            out = pol.run_logical_setup(
                "demo", {"rag_collections": [{"name": "eu-normativa"}]}, "davide")
        self.assertIn("rag_collection:eu-normativa", out["done"])
        self.assertEqual(out["gaps"], [])

    def test_initial_resources_are_a_gap_not_an_automatic_ingest(self):
        """rag.ingest richiede un file già DENTRO un topic (tier+name+path):
        un path del pack o un url non ce l'hanno. Deciso: gap dichiarato, non
        un topic di comodo inventato qui."""
        with patch.object(pol.gateway_pdp, "gw_tool",
                          side_effect=_gw({"rag.create_collection": (200, {"result": {}})})):
            out = pol.run_logical_setup(
                "demo", {"rag_collections": [
                    {"name": "eu-normativa", "resources": [{"path": "x.pdf"}]}]},
                "davide")
        gap = [g for g in out["gaps"] if g["kind"] == "rag_ingest"]
        self.assertEqual(len(gap), 1)
        self.assertEqual(gap[0]["collection"], "eu-normativa")


class NoOpTests(unittest.TestCase):
    def test_empty_declarations_produce_no_calls_and_no_gaps(self):
        with patch.object(pol.gateway_pdp, "gw_tool") as gw:
            out = pol.run_logical_setup("demo", {}, "davide")
        gw.assert_not_called()
        self.assertEqual(out, {"name": "demo", "done": [], "gaps": []})


class AsyncWrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_async_wrapper_does_not_block_the_event_loop(self):
        """Non testa il threading in sé (impraticabile senza un vero I/O
        bloccante), solo che il wrapper esista e produca lo stesso risultato
        della funzione sincrona che avvolge."""
        with patch.object(pol.gateway_pdp, "gw_tool",
                          side_effect=_gw({"packs.install_pip":
                                          (200, {"result": {"ok": True}})})):
            out = await pol.run_logical_setup_async(
                "demo", {"requires": {"pip": ["mcp"]}}, "davide")
        self.assertEqual(out["done"], ["pip:mcp"])


if __name__ == "__main__":
    unittest.main()
