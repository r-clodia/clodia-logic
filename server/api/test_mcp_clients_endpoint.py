"""Chi può coniare il token di un client MCP, e per chi.

L'endpoint non decide il tier — quello sta in un punto solo, nel gateway, ed è
giusto che ci resti. Qui si decide una cosa più semplice e più facile da
sbagliare: **chi può chiedere**.

Due permessi, per due ragioni diverse. L'**owner** per chiunque partecipi,
perché è lui che invita. **Ciascuno per sé**, perché quella è la sua identità e
non ha senso che debba farsela dare — un partecipante che si conia il proprio
token non allarga niente, usa quello che ha già.

E un rifiuto: nessuno dei due può coniarlo per chi nel topic non c'è. Non è una
formalità — un token per una stanza di cui non si fa parte verrebbe rifiutato a
ogni chiamata, e la persona lo scoprirebbe dopo aver configurato il client.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from . import topics as T


META = {"tier": "SEAL-1", "owner": "davide",
        "participants": {"davide": "owner", "giovanni": "member"}}


class _Req:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


class _Client:
    """Il gateway, in finta: registra cosa gli è stato chiesto."""

    class TopicsClientError(RuntimeError):
        pass

    def __init__(self):
        self.chiesto = None

    def open_topic(self, tier, name):
        return {"meta": META}

    def mcp_clients(self, tier, name, payload=None):
        self.chiesto = payload
        return {"id": "mcp_x", "token": "ckt1.a.b"}


def _chiama(chi, body):
    """Esegue l'endpoint come se a chiamare fosse `chi`."""
    cl = _Client()
    with patch.object(T, "topics_client", cl), \
         patch.object(T, "_principal_from_request", lambda r: chi), \
         patch.object(T, "_principal_clearance", lambda n: "SEAL-1"), \
         patch.object(T.admin, "is_admin", lambda n: n == "davide"):
        res = asyncio.run(T.issue_mcp_client("SEAL-1", "acme", _Req(body)))
    return res, cl.chiesto


class WhoMayAskTests(unittest.TestCase):
    def test_a_participant_may_mint_for_themselves(self):
        _, chiesto = _chiama("giovanni", {"provider": "anthropic-api"})
        self.assertEqual(chiesto["principal"], "giovanni")
        self.assertEqual(chiesto["by"], "giovanni")

    def test_the_owner_may_mint_for_a_participant(self):
        _, chiesto = _chiama("davide", {"principal": "giovanni",
                                        "provider": "anthropic-api"})
        self.assertEqual(chiesto["principal"], "giovanni")
        self.assertEqual(chiesto["by"], "davide")

    def test_a_participant_may_not_mint_for_someone_else(self):
        """Il caso che rende utile la regola: senza, Giovanni potrebbe farsi dare
        un token a nome di Matteo e parlare come lui."""
        with self.assertRaises(HTTPException) as e:
            _chiama("giovanni", {"principal": "davide", "provider": "p"})
        self.assertEqual(e.exception.status_code, 403)

    def test_nobody_may_mint_for_a_stranger(self):
        with self.assertRaises(HTTPException) as e:
            _chiama("davide", {"principal": "estraneo", "provider": "p"})
        self.assertEqual(e.exception.status_code, 403)
        self.assertIn("non partecipa", e.exception.detail)

    def test_an_anonymous_request_is_refused(self):
        with patch.object(T, "_principal_from_request", lambda r: None):
            with self.assertRaises(HTTPException) as e:
                asyncio.run(T.issue_mcp_client("SEAL-1", "acme", _Req({})))
        self.assertEqual(e.exception.status_code, 401)


class WhatTravelsTests(unittest.TestCase):
    def test_the_role_and_clearance_come_from_the_platform(self):
        """Non dal corpo della richiesta. Se arrivassero da lì, chiunque potrebbe
        chiedersi un token da admin con clearance SEAL-4 — il ruolo firmato
        varrebbe quanto la parola di chi lo chiede."""
        _, chiesto = _chiama("giovanni", {"provider": "p", "human_role": "admin",
                                          "clearance": "SEAL-4"})
        self.assertEqual(chiesto["human_role"], "user")
        self.assertEqual(chiesto["clearance"], "SEAL-1")

    def test_the_owner_is_recognised_as_admin_only_if_they_are(self):
        _, chiesto = _chiama("davide", {"provider": "p"})
        self.assertEqual(chiesto["human_role"], "admin")

    def test_only_the_owner_can_vouch_for_the_tier(self):
        """Il consenso al tier è l'assunzione di una dichiarazione che nessuno
        può verificare. Un partecipante che se la desse da solo trasformerebbe la
        cautela in una casella da spuntare."""
        _, chiesto = _chiama("giovanni", {"provider": "p", "tier_consent": True})
        self.assertFalse(chiesto["tier_consent"])
        _, chiesto = _chiama("davide", {"provider": "p", "tier_consent": True})
        self.assertTrue(chiesto["tier_consent"])


class RevokeTests(unittest.TestCase):
    def test_a_person_may_revoke_their_own(self):
        _, chiesto = _chiama("giovanni", {"action": "revoke", "id": "mcp_x",
                                          "principal": "giovanni"})
        self.assertEqual(chiesto["action"], "revoke")

    def test_a_person_may_not_revoke_someone_elses(self):
        with self.assertRaises(HTTPException) as e:
            _chiama("giovanni", {"action": "revoke", "id": "mcp_y",
                                 "principal": "davide"})
        self.assertEqual(e.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
