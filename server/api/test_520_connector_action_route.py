"""L'azione di un connettore su un topic, generica (clodia-platform#520).

Il gateway esegue già le azioni che un manifest dichiara (#519), ma la sua rotta
vive sotto `/internal/`, che il browser non raggiunge: la webui parla con
l'agent-server. Questa è quella porta — la forma generica di `tg-link` e
`mailbox-link`, che restano come alias finché la webui parla quella lingua.

Le due cose che possono degradare in silenzio, e che qui si tengono ferme:

1. **la rotta generica non è la porta di servizio di quella specifica.**
   `mailbox-link` è da admin di piattaforma dal 27 set — una casella di sistema
   è posta che non è dell'owner della stanza. Se l'azione generica non leggesse
   `admin_only`, lo stesso collegamento passerebbe dall'altra URL con il solo
   controllo di owner, e nessuno se ne accorgerebbe perché il risultato è
   identico;
2. **i campi si inoltrano senza conoscerli.** Un client che elencasse i nomi
   dei campi per filtrarli riporterebbe dentro il per-connettore che questa
   rotta esiste per togliere: il giorno che un pack dichiara un campo nuovo,
   quel campo sparirebbe per strada e il sintomo sarebbe «l'azione non fa
   niente».
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from . import topics as T

META = {"tier": "SEAL-1", "owner": "giovanni",
        "participants": {"giovanni": "owner", "mario": "member"}}


class _Req:
    def __init__(self, body=None):
        self._b = body

    async def json(self):
        if self._b is None:
            raise ValueError("nessun corpo")
        return self._b


class _Client:
    """Il gateway, per quel poco che questa rotta gli chiede."""

    class TopicsClientError(RuntimeError):
        def __init__(self, msg="gateway", status=None, detail=None):
            super().__init__(msg)
            self.status = status
            self.detail = detail

        @property
        def is_client_error(self):
            return bool(self.status and 400 <= self.status < 500)

    def __init__(self, *, admin_only=False, stato_errore=None):
        self.chiamate: list = []
        self.admin_only = admin_only
        self.stato_errore = stato_errore

    def open_topic(self, tier, name):
        return {"meta": META}

    def connector_action_status(self, tier, name, connector, action):
        self.chiamate.append(("status", tier, name, connector, action))
        if self.stato_errore is not None:
            raise self.stato_errore
        return {"connector": connector, "action": action,
                "admin_only": self.admin_only, "fields": [],
                "entries": {"ingress": [], "egress": []}, "tier": tier}

    def connector_action_apply(self, tier, name, connector, action, body):
        self.chiamate.append(("apply", tier, name, connector, action, body))
        return {"connector": connector, "action": action, "connected": True}

    def __getattr__(self, nome):
        if not nome.startswith("async_"):
            raise AttributeError(nome)
        sync = getattr(self, nome[len("async_"):])

        async def chiamata(*a, **k):
            return sync(*a, **k)

        return chiamata


class _Base(unittest.TestCase):
    ADMINS = {"davide"}

    def esegui(self, coro, chi="giovanni", cli=None):
        self.cli = cli or _Client()
        p = [patch.object(T, "topics_client", self.cli),
             patch.object(T, "_principal_from_request", lambda r: chi),
             patch.object(T.admin, "is_admin", lambda n: n in self.ADMINS)]
        for x in p:
            x.start()
        try:
            return asyncio.run(coro)
        finally:
            for x in reversed(p):
                x.stop()


class OrdinaryActionTests(_Base):
    def test_the_owner_reads_the_state_of_an_action(self):
        res = self.esegui(T.connector_action_status(
            "SEAL-1", "acme", "telegram", "telegram-link", _Req()))
        self.assertEqual(res["connector"], "telegram")
        self.assertEqual(self.cli.chiamate,
                         [("status", "SEAL-1", "acme", "telegram", "telegram-link")])

    def test_the_owner_applies_it(self):
        res = self.esegui(T.connector_action_apply(
            "SEAL-1", "acme", "telegram", "telegram-link",
            _Req({"action": "connect", "chat": "-1001234567890"})))
        self.assertTrue(res["connected"])
        self.assertEqual(self.cli.chiamate[-1],
                         ("apply", "SEAL-1", "acme", "telegram", "telegram-link",
                          {"action": "connect", "chat": "-1001234567890"}))

    def test_the_declared_fields_travel_untouched(self):
        """Compresi quelli che questo repository non ha mai sentito nominare:
        è ciò che permette a un connettore di pack di funzionare senza un
        deploy della webui né dell'agent-server."""
        corpo = {"action": "connect", "campo_di_un_pack_futuro": "valore",
                 "accept_seal_downgrade": True, "numero": 3}
        self.esegui(T.connector_action_apply(
            "SEAL-1", "acme", "sms", "sms-link", _Req(dict(corpo))))
        self.assertEqual(self.cli.chiamate[-1][-1], corpo)

    def test_a_participant_who_is_not_the_owner_is_refused(self):
        with self.assertRaises(HTTPException) as e:
            self.esegui(T.connector_action_apply(
                "SEAL-1", "acme", "telegram", "telegram-link",
                _Req({"action": "connect", "chat": "-100"})), chi="mario")
        self.assertEqual(e.exception.status_code, 403)
        self.assertEqual(self.cli.chiamate, [])

    def test_an_unknown_verb_is_a_400_before_the_gateway(self):
        """Un corpo malformato è un rifiuto di questo lato, non un 502 che
        nomina un guasto del gateway che non c'è stato."""
        for corpo in ({"action": "forse"}, {}, {"action": ""}):
            with self.subTest(corpo=corpo):
                with self.assertRaises(HTTPException) as e:
                    self.esegui(T.connector_action_apply(
                        "SEAL-1", "acme", "telegram", "telegram-link", _Req(corpo)))
                self.assertEqual(e.exception.status_code, 400)
                self.assertEqual(self.cli.chiamate, [])

    def test_a_refusal_of_the_gateway_keeps_its_own_status(self):
        """Un connettore inesistente è 404, non 502: il 502 dice «il gateway è
        rotto» e manderebbe a cercare un guasto che non c'è."""
        cli = _Client(stato_errore=_Client.TopicsClientError(
            "connettore 'ignoto' non installato", status=404,
            detail="connettore 'ignoto' non installato"))
        with self.assertRaises(HTTPException) as e:
            self.esegui(T.connector_action_status(
                "SEAL-1", "acme", "ignoto", "x", _Req()), cli=cli)
        self.assertEqual(e.exception.status_code, 404)


class AdminOnlyActionTests(_Base):
    """La regola di `mailbox-link` non si perde passando per la rotta generica."""

    def _cli(self):
        return _Client(admin_only=True)

    def test_a_non_admin_owner_may_not_apply_an_admin_only_action(self):
        with self.assertRaises(HTTPException) as e:
            self.esegui(T.connector_action_apply(
                "SEAL-1", "acme", "mail", "mailbox-link",
                _Req({"action": "connect", "account": "studio"})),
                chi="giovanni", cli=self._cli())
        self.assertEqual(e.exception.status_code, 403)
        # Lo stato lo ha letto (è così che sa che serve un admin), ma non ha
        # applicato nulla: è la metà che conta.
        self.assertEqual([c[0] for c in self.cli.chiamate], ["status"])

    def test_a_non_admin_owner_does_not_even_see_its_state(self):
        """L'elenco delle caselle nomina gli indirizzi dell'istanza: è già una
        informazione, non solo un comando."""
        with self.assertRaises(HTTPException) as e:
            self.esegui(T.connector_action_status(
                "SEAL-1", "acme", "mail", "mailbox-link", _Req()),
                chi="giovanni", cli=self._cli())
        self.assertEqual(e.exception.status_code, 403)

    def test_an_admin_applies_it(self):
        res = self.esegui(T.connector_action_apply(
            "SEAL-1", "acme", "mail", "mailbox-link",
            _Req({"action": "connect", "account": "studio"})),
            chi="davide", cli=self._cli())
        self.assertTrue(res["connected"])
        self.assertEqual([c[0] for c in self.cli.chiamate], ["status", "apply"])

    def test_an_ordinary_action_stays_open_to_the_owner(self):
        """Il controllo opposto: se `admin_only` finisse cablato a vero, ogni
        bottone del topic diventerebbe da admin e questo test sarebbe l'unico
        a dirlo."""
        res = self.esegui(T.connector_action_apply(
            "SEAL-1", "acme", "telegram", "telegram-link",
            _Req({"action": "connect", "chat": "-100"})), chi="giovanni")
        self.assertTrue(res["connected"])


class ClientShapeTests(unittest.TestCase):
    def test_the_async_wrappers_exist(self):
        """Un handler `async def` chiama SEMPRE il lato async: la versione
        sincrona dentro il loop bloccherebbe il processo, e c'è un guardrail
        sull'AST che lo vieta."""
        from . import topics_client
        for nome in ("async_connector_action_status", "async_connector_action_apply"):
            self.assertTrue(callable(getattr(topics_client, nome, None)), nome)


if __name__ == "__main__":
    unittest.main()
