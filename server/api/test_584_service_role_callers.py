"""clodia-platform#584 — the agent-server's own gateway calls carry the service role.

The gateway's internal routes no longer accept a bare token on `clodia`: a
spawn of `clodia` holds exactly such a token. Every agent-server client that
called them with `mint_session_token(_PRINCIPAL, ttl)` must now mint the signed
`role: agent-server` (clodia-platform#539 §1), or the service stops in
production the moment the gateway is updated. `topics_client`, `mail_courier`
and `gateway_admin` already did; this module pins the six that did not.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ..colony import pki
from . import gate, git_client, imagegen_client, profile, provider_store, rag_store


class _Mint:
    """Records the claims each mint asks for; returns a dummy token."""

    def __init__(self):
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        return "ckt1.dummy"


def _response(status: int = 200, body: dict | None = None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    r.content = b""
    r.text = ""
    return r


class TheAgentServerClientsMintTheServiceRoleTests(unittest.TestCase):

    def _assert_service(self, mint: _Mint) -> None:
        self.assertTrue(mint.calls, "no token minted")
        for _a, kw in mint.calls:
            self.assertEqual(kw.get("role"), pki.ROLE_AGENT_SERVER, kw)
            # The shape the role is signed for: no turn claims next to it.
            for claim in ("on_behalf", "execution_id", "chat"):
                self.assertFalse(kw.get(claim), f"{claim} next to the role: {kw}")

    def test_provider_store(self):
        mint = _Mint()
        with patch.object(pki, "mint_session_token", mint):
            provider_store._headers()
        self._assert_service(mint)

    def test_rag_store(self):
        mint = _Mint()
        with patch.object(pki, "mint_session_token", mint):
            rag_store._headers()
        self._assert_service(mint)

    def test_git_client_vault_read(self):
        mint = _Mint()
        with patch.object(pki, "mint_session_token", mint), \
                patch.object(git_client.requests, "get",
                             return_value=_response(200, {"value": "x"})):
            self.assertEqual(git_client.read_credential("github_pat"), "x")
        self._assert_service(mint)

    def test_imagegen_client(self):
        mint = _Mint()
        with patch.object(pki, "mint_session_token", mint), \
                patch.object(imagegen_client.requests, "post", return_value=_response()):
            imagegen_client.generate("a tomato")
        self._assert_service(mint)

    def test_profile_declares_the_person_next_to_the_role(self):
        mint = _Mint()
        with patch.object(pki, "mint_session_token", mint):
            h = profile._headers("davide")
        self._assert_service(mint)
        self.assertEqual(h["X-Clodia-Principal"], "davide")

    def test_gate_keeps_the_deciding_human_next_to_the_role(self):
        mint = _Mint()
        with patch.object(pki, "mint_session_token", mint), \
                patch.object(gate.requests, "request", return_value=_response()):
            gate._gw("GET", "/pending", "davide")
        self._assert_service(mint)
        self.assertEqual(mint.calls[0][1].get("principal"), "davide")


if __name__ == "__main__":
    unittest.main()
