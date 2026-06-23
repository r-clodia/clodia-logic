"""Test PKI colonia + auth keystore.

Esecuzione: ``python3 -m unittest server.colony.test_pki -v``
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from . import pki


class PkiBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = {
            "CA_DIR": pki.CA_DIR, "CA_KEY": pki.CA_KEY, "CA_CRT": pki.CA_CRT,
            "AGENT_SECRETS": pki.AGENT_SECRETS, "PKI_DIR": pki.PKI_DIR,
            "CERTS_DIR": pki.CERTS_DIR, "REVOKED_FILE": pki.REVOKED_FILE,
        }
        pki.CA_DIR = root / "secrets" / "ca"
        pki.CA_KEY = pki.CA_DIR / "ca.key"
        pki.CA_CRT = pki.CA_DIR / "ca.crt"
        pki.AGENT_SECRETS = root / "secrets" / "agents"
        pki.PKI_DIR = root / "pki"
        pki.CERTS_DIR = pki.PKI_DIR / "certs"
        pki.REVOKED_FILE = pki.PKI_DIR / "revoked.json"

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            setattr(pki, k, v)
        self.tmp.cleanup()


class PkiTests(PkiBase):
    def test_ca_init_idempotent(self):
        self.assertFalse(pki.ca_initialized())
        p1 = pki.init_ca()
        first = p1.read_bytes()
        p2 = pki.init_ca()
        self.assertEqual(first, p2.read_bytes())  # non rigenera
        self.assertTrue(pki.ca_initialized())

    def test_issue_and_token_roundtrip(self):
        pki.init_ca()
        pki.issue_agent_identity("dairio")
        self.assertTrue(pki.has_identity("dairio"))
        token = pki.mint_session_token("dairio", execution_id="exec123")
        payload = pki.verify_session_token(token)
        self.assertEqual(payload["agent"], "dairio")
        self.assertEqual(payload["execution_id"], "exec123")

    def test_external_key_cert_not_overwritten(self):
        """Un principal a chiave esterna (umano): cert emesso da pubkey, nessuna
        key lato server. issue_agent_identity (come fa issue-all al boot) NON
        deve rigenerare/sovrascrivere il cert → la recovery key resta valida."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        pki.init_ca()
        priv = Ed25519PrivateKey.generate()
        pub_pem = priv.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        pki.issue_cert_for_pubkey("umano", pub_pem)
        cert_before = pki.agent_cert_path("umano").read_bytes()
        self.assertFalse(pki.agent_key_path("umano").is_file())  # no key lato server

        # simula issue-all al boot
        pki.issue_agent_identity("umano")

        self.assertEqual(cert_before, pki.agent_cert_path("umano").read_bytes())
        self.assertFalse(pki.agent_key_path("umano").is_file())

    def test_issue_without_ca_fails(self):
        with self.assertRaises(RuntimeError):
            pki.issue_agent_identity("dairio")

    def test_revoked_agent_rejected(self):
        pki.init_ca()
        pki.issue_agent_identity("satia")
        token = pki.mint_session_token("satia")
        pki.revoke("satia")
        with self.assertRaises(PermissionError):
            pki.verify_session_token(token)
        # nuova emissione riabilita
        pki.issue_agent_identity("satia", force=True)
        payload = pki.verify_session_token(pki.mint_session_token("satia"))
        self.assertEqual(payload["agent"], "satia")

    def test_expired_token_rejected(self):
        pki.init_ca()
        pki.issue_agent_identity("ailon")
        token = pki.mint_session_token("ailon", ttl_seconds=-1)
        with self.assertRaises(PermissionError):
            pki.verify_session_token(token)

    def test_tampered_token_rejected(self):
        pki.init_ca()
        pki.issue_agent_identity("ailon")
        pki.issue_agent_identity("jainsen")
        token = pki.mint_session_token("ailon")
        prefix, body, sig = token.split(".")
        # impersonificazione: payload di jainsen con firma di ailon
        forged_payload = pki._b64e(
            b'{"agent":"jainsen","execution_id":"","iat":%d,"exp":%d,"aud":"keystore"}'
            % (int(time.time()), int(time.time()) + 600))
        with self.assertRaises(PermissionError):
            pki.verify_session_token(f"{prefix}.{forged_payload}.{sig}")

    def test_unknown_agent_rejected(self):
        pki.init_ca()
        with self.assertRaises(PermissionError):
            pki.mint_session_token("fantasma")
        # token costruito a mano per agente senza cert
        pki.issue_agent_identity("ailon")
        token = pki.mint_session_token("ailon")
        # cert cancellato dopo l'emissione → verify fallisce
        pki.agent_cert_path("ailon").unlink()
        with self.assertRaises(PermissionError):
            pki.verify_session_token(token)

    def test_malformed_token_rejected(self):
        pki.init_ca()
        for bad in ("", "x", "ckt1.abc", "wrong.a.b", "ckt1.!!.??"):
            with self.assertRaises(PermissionError):
                pki.verify_session_token(bad)


if __name__ == "__main__":
    unittest.main()
