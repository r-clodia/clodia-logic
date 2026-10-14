"""L'agent-server si dichiara, non si deduce (#539 §1).

Il gateway riconosceva le chiamate che l'agent-server fa PER SÉ dal fatto che il
token non portava né `execution_id` né `chat` — cioè per sottrazione. È sicuro
solo finché ogni token d'agente porta almeno uno dei due, e `_execution_id(spawn)`
torna stringa vuota quando il workspace dello spawn non è stato creato: la
sicurezza di una rotta che amministra i muri di un topic poggiava su un dettaglio
di un'altra funzione.

Qui si misura la metà di clodia-logic: il claim `role` esiste, è FIRMATO, non è
inventabile a piacere, e i due client che parlano al gateway per conto del
servizio lo mandano. L'altra metà — il gateway che lo pretende — sta in
clodia-tools.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import pki


class _PkiTmp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = {k: getattr(pki, k) for k in
                       ("CA_DIR", "CA_KEY", "CA_CRT", "AGENT_SECRETS", "PKI_DIR",
                        "CERTS_DIR", "REVOKED_FILE")}
        pki.CA_DIR = root / "secrets" / "ca"
        pki.CA_KEY = pki.CA_DIR / "ca.key"
        pki.CA_CRT = pki.CA_DIR / "ca.crt"
        pki.AGENT_SECRETS = root / "secrets" / "agents"
        pki.PKI_DIR = root / "pki"
        pki.CERTS_DIR = pki.PKI_DIR / "certs"
        pki.REVOKED_FILE = pki.PKI_DIR / "revoked.json"
        from .. import audit_events
        p = patch.object(audit_events, "report_sync", lambda ev: True)
        p.start()
        self.addCleanup(p.stop)
        # La cache dei mint è globale al modulo: un token finto lasciato lì da
        # un test lo riceverebbe il test dopo (ed è successo).
        pki._MINT_CACHE.clear()
        self.addCleanup(pki._MINT_CACHE.clear)
        pki.init_ca()
        pki.issue_agent_identity("clodia")

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            setattr(pki, k, v)
        self.tmp.cleanup()


class RoleClaimTests(_PkiTmp):
    def test_the_role_is_signed_into_the_token(self) -> None:
        token = pki.mint_session_token("clodia", role=pki.ROLE_AGENT_SERVER)
        self.assertEqual(pki.ROLE_AGENT_SERVER,
                         pki.verify_session_token(token).get("role"))

    def test_a_token_without_the_role_does_not_carry_it(self) -> None:
        """L'assenza resta l'assenza: un token d'agente non diventa di servizio
        perché gli mancano altri claim."""
        token = pki.mint_session_token("clodia")
        self.assertIsNone(pki.verify_session_token(token).get("role"))

    def test_an_unknown_role_is_refused_where_the_key_is(self) -> None:
        """Non è un sistema di ruoli: c'è un valore solo. Un valore diverso è un
        errore di programmazione, e va scoperto al mint — non a valle, su un
        token già firmato che qualcuno userà."""
        with self.assertRaises(PermissionError):
            pki.mint_session_token("clodia", role="admin")

    def test_the_role_cannot_be_smuggled_in_by_the_cache(self) -> None:
        """`role` entra nella chiave di cache del mint: un token di servizio non
        può essere riusato come token d'agente dello stesso principal, e
        viceversa."""
        chiavi = []

        def _finto(agent, execution_id, ttl_seconds, principal, clearance,
                   on_behalf, human_role, chat, scoped_key, unattended,
                   scope_tier=None, origin=None, role=None):
            chiavi.append(role)
            return f"ckt1.finto-{role}.x"

        with patch.dict("os.environ", {"CLODIA_ORCHESTRATOR_SECRET": "s"}), \
             patch.object(pki, "_mint_request", _finto):
            a = pki.mint_session_token("clodia", role=pki.ROLE_AGENT_SERVER)
            b = pki.mint_session_token("clodia")
        self.assertNotEqual(a, b)
        self.assertEqual([pki.ROLE_AGENT_SERVER, None], chiavi)


class ServiceClientsTests(unittest.TestCase):
    """I due client che parlano al gateway per conto del SERVIZIO lo dichiarano.

    Sono quelli che toccano le rotte strette da questa issue: i muri di un topic
    (`topics_client`) e la validazione dei connettori di un pack, dietro il
    controllo admin delle rotte Tools (`gateway_admin`). Gli altri client
    dell'agent-server passano da `internal_auth`, che non cambia.
    """

    def _token_mintato(self, modulo) -> dict:
        visti: dict = {}

        def _mint(agent, **kw):
            visti.update(kw)
            visti["agent"] = agent
            return "ckt1.x.y"

        with patch.object(modulo.pki, "mint_session_token", _mint):
            headers = modulo._headers()
        self.assertEqual("Bearer ckt1.x.y", headers["Authorization"])
        return visti

    def test_topics_client_declares_the_service_role(self) -> None:
        from ..api import topics_client
        self.assertEqual(pki.ROLE_AGENT_SERVER,
                         self._token_mintato(topics_client).get("role"))

    def test_gateway_admin_declares_the_service_role(self) -> None:
        from ..api import gateway_admin
        self.assertEqual(pki.ROLE_AGENT_SERVER,
                         self._token_mintato(gateway_admin).get("role"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
