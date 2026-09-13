"""#342 — la pagina Databases naviga i dati, non solo l'inventario.

L'elenco dei datastore c'era già (`GET /clodia/datastores`); mancava tutto ciò
che sta DENTRO: le tabelle di un datastore, una pagina delle sue righe, e i
documenti iniettati in una collection RAG (di cui si vedeva solo il conteggio).

Le rotte nuove non aprono SQLite qui: inoltrano al gateway, che ha già
connessione read-only, whitelist per tipo di istruzione, tetto in byte e audit
— e che sopra di esse applica il PDP (clodia-tools#275). Questi test guardano
ciò che è responsabilità di QUESTO lato: l'identificatore di tabella, i limiti
della paginazione e la forma della risposta.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from fastapi import HTTPException

from . import datastores, gateway_pdp, rag_store


def _req():
    return mock.Mock(headers={}, client=None)


class _Gateway:
    """Il gateway finto: registra le chiamate e risponde come quello vero.

    `gw_tool` e non `forward`: così il test percorre `forward` davvero — la
    verifica del principal compresa — invece di saltarla.
    """

    def __init__(self, risposte: dict):
        self.risposte, self.chiamate = risposte, []

    def __call__(self, tool: str, arguments: dict, principal: str):
        self.chiamate.append((tool, arguments, principal))
        query = arguments.get("query", "")
        for frammento, payload in self.risposte.items():
            if frammento in query:
                return 200, {"result": payload}
        return 200, {"result": {"columns": [], "rows": [], "truncated": False}}


_TABELLE = {"columns": ["name", "type"],
            "rows": [{"name": "contacts", "type": "table"},
                     {"name": "note", "type": "view"}],
            "truncated": False}


class RowsAndTablesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_gw = gateway_pdp.gw_tool
        self._old_principal = gateway_pdp._principal_from_request
        gateway_pdp._principal_from_request = lambda _r: "davide"
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        gateway_pdp.gw_tool = self._old_gw
        gateway_pdp._principal_from_request = self._old_principal

    def _gateway(self, righe=None):
        gw = _Gateway({"sqlite_master": _TABELLE,
                       "FROM \"contacts\"": righe if righe is not None else {
                           "columns": ["id", "nome"],
                           "rows": [{"id": 1, "nome": "Anna"}],
                           "truncated": False}})
        gateway_pdp.gw_tool = gw
        return gw

    def test_tables_lists_the_names_of_the_datastore(self) -> None:
        self._gateway()
        out = asyncio.run(datastores.datastore_tables("base-pack", "contacts", _req()))
        self.assertEqual("base-pack/contacts", out["datastore"])
        self.assertEqual(["contacts", "note"], out["tables"])

    def test_rows_returns_a_page_of_the_requested_table(self) -> None:
        gw = self._gateway()
        out = asyncio.run(datastores.datastore_rows(
            "base-pack", "contacts", _req(), table="contacts", limit=50, offset=0))
        self.assertEqual(["id", "nome"], out["columns"])
        self.assertEqual([{"id": 1, "nome": "Anna"}], out["rows"])
        self.assertFalse(out["has_more"])
        # La query di lettura è parametrizzata su limite e scostamento: i
        # numeri non finiscono nel testo dell'istruzione.
        _tool, args, principal = gw.chiamate[-1]
        self.assertEqual("datastore.read", _tool)
        self.assertEqual("davide", principal)
        self.assertIn("LIMIT ? OFFSET ?", args["query"])
        self.assertEqual([51, 0], args["params"])

    def test_an_unknown_table_is_refused_before_any_query(self) -> None:
        """Un identificatore non è parametrizzabile in SQL: l'unica difesa è
        non costruire l'istruzione se il nome non è fra quelli del datastore."""
        gw = self._gateway()
        with self.assertRaises(HTTPException) as e:
            asyncio.run(datastores.datastore_rows(
                "base-pack", "contacts", _req(), table="contacts; DROP TABLE note"))
        self.assertEqual(400, e.exception.status_code)
        self.assertEqual(
            ["datastore.read"], [c[0] for c in gw.chiamate],
            "solo l'elenco tabelle: nessuna query costruita col nome rifiutato")
        self.assertIn("sqlite_master", gw.chiamate[0][1]["query"])

    def test_the_page_size_has_a_ceiling_and_the_offset_a_floor(self) -> None:
        gw = self._gateway()
        asyncio.run(datastores.datastore_rows(
            "base-pack", "contacts", _req(), table="contacts", limit=10_000, offset=-5))
        args = gw.chiamate[-1][1]
        self.assertEqual([datastores._MAX_ROWS + 1, 0], args["params"])

    def test_has_more_comes_from_the_extra_row_not_from_a_count(self) -> None:
        """Si chiede una riga in più di quelle mostrate: la pagina sa che ce
        n'è un'altra senza un `count(*)` su una tabella che può essere grande.
        La riga in più non deve arrivare al client."""
        self._gateway(righe={"columns": ["id"],
                             "rows": [{"id": 1}, {"id": 2}, {"id": 3}],
                             "truncated": False})
        out = asyncio.run(datastores.datastore_rows(
            "base-pack", "contacts", _req(), table="contacts", limit=2, offset=0))
        self.assertEqual([{"id": 1}, {"id": 2}], out["rows"])
        self.assertTrue(out["has_more"])

    def test_a_refusal_from_the_gateway_stays_a_refusal(self) -> None:
        """Il PDP è là: se nega, qui non si riprova e non si ripiega su una
        lettura locale del file."""
        def _nega(tool, arguments, principal):
            return 403, {"detail": "riservato agli admin"}
        gateway_pdp.gw_tool = _nega
        with self.assertRaises(HTTPException) as e:
            asyncio.run(datastores.datastore_tables("base-pack", "contacts", _req()))
        self.assertEqual(403, e.exception.status_code)


class RagDocumentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_authz = gateway_pdp.require_authz
        self._old_docs = rag_store.list_documents
        gateway_pdp.require_authz = lambda *a, **k: "davide"
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        gateway_pdp.require_authz = self._old_authz
        rag_store.list_documents = self._old_docs

    def test_lists_the_documents_injected_in_the_collection(self) -> None:
        visto = {}

        def _docs(collection):
            visto["collection"] = collection
            return [{"name": "GDPR", "version": "2016/679", "chunks": 120}]
        rag_store.list_documents = _docs

        out = asyncio.run(datastores.rag_collection_documents("eu-normativa", _req()))
        self.assertEqual("eu-normativa", visto["collection"])
        self.assertEqual("eu-normativa", out["collection"])
        self.assertEqual(1, len(out["documents"]))

    def test_rag_service_down_is_503_not_an_empty_list(self) -> None:
        """Un elenco vuoto direbbe «questa collection non ha documenti», che è
        un'altra affermazione — e falsa. L'inventario delle collection può
        degradare (la pagina resta leggibile); il contenuto di UNA collection
        chiesto apposta, no."""
        def _boom(collection):
            raise rag_store.RagStoreError("gateway giù")
        rag_store.list_documents = _boom

        with self.assertRaises(HTTPException) as e:
            asyncio.run(datastores.rag_collection_documents("eu-normativa", _req()))
        self.assertEqual(503, e.exception.status_code)


class HumanClearanceInTheTokenTests(unittest.TestCase):
    """La clearance della PERSONA nel token on-behalf.

    Senza questo claim il gateway legge `None` e lo tratta come SEAL-0
    (`_rank(None)`): l'asse livello dell'autorizzazione umana esiste ma decide
    sempre «il minimo», e un datastore SEAL-1 resta invisibile anche all'owner.
    """

    def test_the_token_carries_the_declared_clearance_of_the_principal(self) -> None:
        visto = {}

        def _mint(agent, **kw):
            visto.update(kw)
            return "ckt1.finto"

        spec = mock.Mock(clearance="SEAL-2", type="human")
        with mock.patch.object(gateway_pdp.pki, "mint_session_token", _mint), \
                mock.patch.object(gateway_pdp.registry, "get_by_name", return_value=spec):
            gateway_pdp._token("davide")
        self.assertEqual("SEAL-2", visto.get("clearance"))
        self.assertTrue(visto.get("on_behalf"))

    def test_an_unknown_principal_gets_no_clearance_claim(self) -> None:
        """Nessuna invenzione: chi non è nella registry non porta un livello, e
        il gateway lo tratta come il minimo. Fail-closed, non un default."""
        visto = {}

        def _mint(agent, **kw):
            visto.update(kw)
            return "ckt1.finto"

        with mock.patch.object(gateway_pdp.pki, "mint_session_token", _mint), \
                mock.patch.object(gateway_pdp.registry, "get_by_name", return_value=None):
            gateway_pdp._token("ignoto")
        self.assertIsNone(visto.get("clearance"))


if __name__ == "__main__":
    unittest.main()
