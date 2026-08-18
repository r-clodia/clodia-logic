"""Chi può coniare il token di un client MCP, e per chi.

L'endpoint non decide il tier — quello sta in un punto solo, nel gateway, ed è
giusto che ci resti. Qui si decide una cosa più semplice e più facile da
sbagliare: **chi può chiedere**.

Due permessi, per due ragioni diverse. L'**owner** per chiunque partecipi,
perché è lui che invita. **Ciascuno per sé**, perché quella è la sua identità e
non ha senso che debba farsela dare — un partecipante che si conia il proprio
token non allarga niente, usa quello che ha già.

E due rifiuti. Nessuno dei due permessi vale per chi nel topic non c'è: un
token per una stanza di cui non si fa parte verrebbe rifiutato a ogni chiamata,
e chi lo chiede lo scoprirebbe dopo aver configurato il client. E dal 18 ago
2026 (#242) l'emissione vale **solo per un proxy**: il config da incollare in un
client MCP di persona non si conia più, perché un sistema terzo entra in una
stanza come partecipante con un nome e un certificato, non come credenziale
copiata a mano. Ciò che è già stato coniato resta elencabile e revocabile: si
spegne l'emissione, non il modo di chiudere quello che è aperto.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from . import topics as T


META = {"tier": "SEAL-1", "owner": "davide",
        "participants": {"davide": "owner", "giovanni": "member",
                         "crm-esterno": "member"}}


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


def _chiama(chi, body, tipo="proxy"):
    """Esegue l'endpoint come se a chiamare fosse `chi`.

    `tipo` è la natura che la registry attribuisce al principal per cui si conia.
    Il default è `proxy` perché da #242 è l'unico caso che l'endpoint conia: i
    test sui permessi parlano di quel caso, non di uno che non esiste più.
    """
    from types import SimpleNamespace
    from ..agents import registry
    cl = _Client()
    spec = SimpleNamespace(type=tipo) if tipo else None
    with patch.object(T, "topics_client", cl), \
         patch.object(T, "_principal_from_request", lambda r: chi), \
         patch.object(T, "_principal_clearance", lambda n: "SEAL-1"), \
         patch.object(registry, "get_by_name", lambda n: spec), \
         patch.object(T.admin, "is_admin", lambda n: n == "davide"):
        res = asyncio.run(T.issue_mcp_client("SEAL-1", "acme", _Req(body)))
    return res, cl.chiesto


class WhoMayAskTests(unittest.TestCase):
    def test_a_participant_may_mint_for_themselves(self):
        _, chiesto = _chiama("crm-esterno", {"provider": "sistema-crm"})
        self.assertEqual(chiesto["principal"], "crm-esterno")
        self.assertEqual(chiesto["by"], "crm-esterno")

    def test_the_owner_may_mint_for_a_participant(self):
        _, chiesto = _chiama("davide", {"principal": "crm-esterno",
                                        "provider": "sistema-crm"})
        self.assertEqual(chiesto["principal"], "crm-esterno")
        self.assertEqual(chiesto["by"], "davide")

    def test_a_participant_may_not_mint_for_someone_else(self):
        """Il caso che rende utile la regola: senza, Giovanni potrebbe farsi dare
        il token del proxy e parlare come lui — ammettere un sistema terzo in una
        stanza è un atto dell'owner."""
        with self.assertRaises(HTTPException) as e:
            _chiama("giovanni", {"principal": "crm-esterno", "provider": "p"})
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
        _, chiesto = _chiama("davide", {"principal": "crm-esterno",
                                        "provider": "p", "human_role": "admin",
                                        "clearance": "SEAL-4"})
        self.assertEqual(chiesto["human_role"], "user")
        self.assertEqual(chiesto["clearance"], "SEAL-1")

    def test_only_the_owner_can_vouch_for_the_tier(self):
        """Il consenso al tier è l'assunzione di una dichiarazione che nessuno
        può verificare. Un proxy che se la desse da solo trasformerebbe la
        cautela in una casella da spuntare — e la dichiarazione riguarda dove
        finisce la conversazione della stanza, cioè le mura."""
        _, chiesto = _chiama("crm-esterno", {"provider": "p",
                                             "tier_consent": True})
        self.assertFalse(chiesto["tier_consent"])
        _, chiesto = _chiama("davide", {"principal": "crm-esterno",
                                        "provider": "p", "tier_consent": True})
        self.assertTrue(chiesto["tier_consent"])


class RevokeTests(unittest.TestCase):
    def test_a_person_may_revoke_their_own(self):
        _, chiesto = _chiama("giovanni", {"action": "revoke", "id": "mcp_x",
                                          "principal": "giovanni"}, tipo="human")
        self.assertEqual(chiesto["action"], "revoke")

    def test_a_person_may_not_revoke_someone_elses(self):
        with self.assertRaises(HTTPException) as e:
            _chiama("giovanni", {"action": "revoke", "id": "mcp_y",
                                 "principal": "davide"}, tipo="human")
        self.assertEqual(e.exception.status_code, 403)

    def test_a_grant_already_minted_for_a_person_stays_revocable(self):
        """Il rifiuto di #242 riguarda l'EMISSIONE. Se prendesse anche la revoca,
        i token coniati finora resterebbero vivi fino alla scadenza senza modo di
        spegnerli — chiudere la porta e buttare la chiave dentro."""
        _, chiesto = _chiama("davide", {"action": "revoke", "id": "mcp_z",
                                        "principal": "giovanni"}, tipo="human")
        self.assertEqual(chiesto["action"], "revoke")


class OnlyAProxyIsMintedTests(unittest.TestCase):
    """Da #242 questa superficie conia per un proxy e per nessun altro.

    La natura del principal decideva soltanto i VERBI del token — dieci per una
    persona, quattro per un proxy. Ora decide anche se il token esiste: il
    pannello «Client MCP» è sparito dalla sidebar perché un sistema terzo entra
    in una stanza come partecipante con un nome, un certificato e un owner che
    l'ha ammesso, non come frammento di configurazione da incollare. Un endpoint
    che continuasse a coniare quel frammento sarebbe un ingresso senza pannello
    di controllo — la lezione di #223, dalla parte opposta.
    """

    def test_a_proxy_is_declared_as_such_and_minted(self):
        _, chiesto = _chiama("davide", {"principal": "crm-esterno",
                                        "provider": "sistema-crm"})
        self.assertEqual(chiesto["principal_kind"], "proxy")

    def test_a_person_is_no_longer_minted(self):
        with self.assertRaises(HTTPException) as e:
            _chiama("giovanni", {"provider": "anthropic-api"}, tipo="human")
        self.assertEqual(e.exception.status_code, 403)
        self.assertIn("proxy", e.exception.detail)

    def test_not_even_the_owner_may_mint_for_a_person(self):
        """Il permesso più largo non riapre una porta chiusa: qui non c'è nulla
        da autorizzare, la credenziale non si emette più per nessuno."""
        with self.assertRaises(HTTPException) as e:
            _chiama("davide", {"principal": "giovanni", "provider": "p"},
                    tipo="human")
        self.assertEqual(e.exception.status_code, 403)

    def test_a_principal_the_registry_does_not_know_is_not_minted(self):
        """La registry che non risponde non deve APRIRE il caso proxy per
        distrazione: senza una natura dichiarata non si conia niente."""
        with self.assertRaises(HTTPException) as e:
            _chiama("davide", {"principal": "crm-esterno", "provider": "p"},
                    tipo=None)
        self.assertEqual(e.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
