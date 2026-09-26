"""Connettore Mailbox di un canale: lo collega solo l'owner
(clodia-platform#406).

Collegare una casella scrive `inbox:<addr>` fra le fonti e `outbox:<addr>` fra
le destinazioni di questa stanza: è un atto sui MURI — da lì in poi i verbi
email di chiunque partecipi potranno leggere e scrivere con quell'indirizzo —
quindi vale la stessa regola del logo, del gruppo Telegram e dei partecipanti.
Un partecipante non owner non decide con quale casella parla la stanza.

Il secondo controllo riguarda il corpo: `account` e `action` si validano QUI e
non solo nel gateway, così una richiesta malformata è un 400 e non un 502 che
nomina un guasto del gateway che non c'è stato.
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
    def __init__(self, body=None):
        self._b = body

    async def json(self):
        if self._b is None:
            raise ValueError("no body")
        return self._b


class _Client:
    class TopicsClientError(RuntimeError):
        pass

    def __init__(self):
        self.chiamate: list = []

    def open_topic(self, tier, name):
        return {"meta": META}

    def mailbox_link_status(self, tier, name):
        self.chiamate.append(("status", tier, name))
        return {"mailboxes": []}

    def mailbox_link_action(self, tier, name, action, **params):
        self.chiamate.append(("action", tier, name, action, params))
        return {"mailboxes": [{"account": params.get("account")}]}

    def __getattr__(self, nome):
        if not nome.startswith("async_"):
            raise AttributeError(nome)
        sync = getattr(self, nome[len("async_"):])

        async def chiamata(*a, **k):
            return sync(*a, **k)

        return chiamata


class _Base(unittest.TestCase):
    def setUp(self):
        self.cli = _Client()

    def come(self, chi: str):
        return [patch.object(T, "topics_client", self.cli),
                patch.object(T, "_principal_from_request", lambda r: chi),
                patch.object(T.admin, "is_admin", lambda n: False)]

    def esegui(self, coro, chi="davide"):
        p = self.come(chi)
        for x in p:
            x.start()
        try:
            return asyncio.run(coro)
        finally:
            for x in reversed(p):
                x.stop()


class OwnerOnlyTests(_Base):
    def test_the_owner_connects_a_mailbox(self):
        res = self.esegui(T.mailbox_link_action(
            "SEAL-1", "acme", _Req({"action": "connect", "account": "studio"})))
        self.assertEqual(res["mailboxes"][0]["account"], "studio")
        self.assertEqual(self.cli.chiamate,
                         [("action", "SEAL-1", "acme", "connect", {"account": "studio"})])

    def test_a_participant_may_not_connect_one(self):
        with self.assertRaises(HTTPException) as e:
            self.esegui(T.mailbox_link_action(
                "SEAL-1", "acme", _Req({"action": "connect", "account": "studio"})),
                chi="giovanni")
        self.assertEqual(e.exception.status_code, 403)
        self.assertEqual(self.cli.chiamate, [])

    def test_a_participant_may_not_disconnect_one_either(self):
        """Simmetrico, come per i partecipanti e le cartelle Drive: chi non può
        aprire un muro non può nemmeno chiuderlo di sua iniziativa."""
        with self.assertRaises(HTTPException) as e:
            self.esegui(T.mailbox_link_action(
                "SEAL-1", "acme", _Req({"action": "disconnect", "account": "studio"})),
                chi="giovanni")
        self.assertEqual(e.exception.status_code, 403)
        self.assertEqual(self.cli.chiamate, [])

    def test_a_participant_does_not_even_see_the_list(self):
        """L'elenco nomina gli indirizzi email dell'istanza: è già una
        informazione, non solo un comando."""
        with self.assertRaises(HTTPException) as e:
            self.esegui(T.mailbox_link_status("SEAL-1", "acme", _Req()), chi="giovanni")
        self.assertEqual(e.exception.status_code, 403)
        self.assertEqual(self.cli.chiamate, [])

    def test_the_owner_sees_the_list(self):
        res = self.esegui(T.mailbox_link_status("SEAL-1", "acme", _Req()))
        self.assertEqual(res, {"mailboxes": []})


class CorpoTests(_Base):
    def test_an_unknown_action_is_a_400_and_reaches_no_gateway(self):
        with self.assertRaises(HTTPException) as e:
            self.esegui(T.mailbox_link_action(
                "SEAL-1", "acme", _Req({"action": "boh", "account": "studio"})))
        self.assertEqual(e.exception.status_code, 400)
        self.assertEqual(self.cli.chiamate, [])

    def test_a_missing_account_is_a_400(self):
        with self.assertRaises(HTTPException) as e:
            self.esegui(T.mailbox_link_action("SEAL-1", "acme", _Req({"action": "connect"})))
        self.assertEqual(e.exception.status_code, 400)
        self.assertEqual(self.cli.chiamate, [])

    def test_a_body_that_is_not_json_is_a_400_not_a_crash(self):
        with self.assertRaises(HTTPException) as e:
            self.esegui(T.mailbox_link_action("SEAL-1", "acme", _Req(None)))
        self.assertEqual(e.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
