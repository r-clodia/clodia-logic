"""PWA pairing tokens are refused at verification (clodia-platform#657).

A pairing token was a `ckt1` session token signed IN THE BROWSER with the
principal's masterkey (clodia-web `mintPairingToken` → `signToken`), with a
30-day lifetime. Its claims are the browser's — `agent`, `execution_id: ""`,
`iat`, `exp`, `aud` — exactly those of a WebUI session, which lives 12 h. The
lifetime is the only difference, and it is enough.

Run: ``python3 -m unittest server.colony.test_657_pairing_token -v``
"""
from __future__ import annotations

import json
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import pki
from .test_pki import PkiBase

HOUR = 3600


class PairingTokenTests(PkiBase):
    def setUp(self) -> None:
        super().setUp()
        pki.init_ca()
        # A human principal: the key lives in the browser, the server only
        # holds the certificate (as `issue_cert_for_pubkey` does at claim).
        self.key = Ed25519PrivateKey.generate()
        pub_pem = self.key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        pki.issue_cert_for_pubkey("davide", pub_pem)

    def _browser_token(self, ttl: int, **extra) -> str:
        """What clodia-web `signToken(principal, mk, ttl)` produces."""
        now = int(time.time())
        payload = {"agent": "davide", "execution_id": "", "iat": now,
                   "exp": now + ttl, "aud": pki.TOKEN_AUDIENCE, **extra}
        body = pki._b64e(json.dumps(payload).encode())
        sig = pki._b64e(self.key.sign(body.encode()))
        return f"{pki.TOKEN_PREFIX}.{body}.{sig}"

    def test_a_pairing_token_is_refused(self):
        token = self._browser_token(30 * 24 * HOUR)  # mintPairingToken default
        with self.assertRaisesRegex(PermissionError, "#657"):
            pki.verify_session_token(token)

    def test_a_pairing_token_does_not_identify_at_login(self):
        token = self._browser_token(30 * 24 * HOUR)
        with self.assertRaisesRegex(PermissionError, "#657"):
            pki.verify_token_against(token, "davide")

    def test_a_webui_session_still_works(self):
        for ttl in (600, 12 * HOUR, 24 * HOUR):  # login probe, session, margin
            with self.subTest(ttl=ttl):
                payload = pki.verify_session_token(self._browser_token(ttl))
                self.assertEqual(payload["agent"], "davide")
                pki.verify_token_against(self._browser_token(ttl), "davide")

    def test_just_over_the_margin_is_refused(self):
        with self.assertRaises(PermissionError):
            pki.verify_session_token(self._browser_token(24 * HOUR + 1))

    def test_a_delegation_is_not_judged_by_this_rule(self):
        # clodia-web `signDelegation` carries `typ`: its lifetime is the
        # delegation's own business, verified by the gateway.
        token = self._browser_token(30 * 24 * HOUR, typ="delegation",
                                    scope={"verb": "*"})
        self.assertEqual(pki.verify_session_token(token)["typ"], "delegation")

    def test_long_server_minted_tokens_are_not_judged_by_this_rule(self):
        # A human MCP client token (clodia-tools `human_mcp.issue`) lives days
        # but carries `principal`, `on_behalf`, `chat` and an execution id.
        pki.issue_agent_identity("clodia")
        token = pki.mint_session_token(
            "clodia", execution_id="mcp_abc", ttl_seconds=30 * 24 * HOUR,
            principal="davide", on_behalf=True, human_role="user",
            chat="chan:SEAL-1:room:davide")
        self.assertEqual(pki.verify_session_token(token)["principal"], "davide")
        # The runtime token is 24 h at most, with an execution id.
        token = pki.mint_session_token("clodia", execution_id="clodia-1",
                                       ttl_seconds=24 * HOUR)
        self.assertEqual(pki.verify_session_token(token)["agent"], "clodia")


if __name__ == "__main__":  # pragma: no cover
    import unittest
    unittest.main()
