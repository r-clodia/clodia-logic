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

#: What `GET /internal/connectors` answers, with the fields the topic page must
#: NOT receive (vault entry names, pack internals, problems).
REGISTRY = {
    "connectors": [
        {"id": "telegram", "label": "Telegram", "pack": "builtin",
         "namespace": "telegram", "builtin": True, "extends": "",
         "schemes": [{"scheme": "tg", "direction": "both", "label": "Chat Telegram",
                      "icon": "", "hierarchical": False, "danger": "",
                      "forms": [{"label": "chat", "example": "-1001234567890"}]}],
         "providers": [{"id": "bot", "label": "Bot", "kind": "form",
                        "vault_entry": "telegram_bot_token", "connected": True,
                        "fields": [{"name": "token", "secret": True}]}],
         "verbs": [{"verb": "telegram.send", "direction": "egress",
                    "scheme": "tg", "prefix": False, "dtype": "telegram"}],
         "topic_actions": [{"id": "telegram-link", "label": "Telegram",
                            "binding": "telegram", "admin_only": False,
                            "fields": [{"name": "chat", "label": "Chat id",
                                        "required": True, "placeholder": "-100…"}],
                            "adds": {"ingress": ["tg:{chat}"], "egress": ["tg:{chat}"]}}],
         "tools_card": {"setup_label": "Collega", "status_verb": "telegram.status",
                        "docs_url": "", "blurb": "b", "scope": "s", "icon": "telegram"},
         "taint": ["tg:"]},
        {"id": "mail", "label": "Mail", "pack": "builtin", "namespace": "email",
         "builtin": True, "extends": "",
         "schemes": [{"scheme": "inbox", "direction": "source", "label": "Casella",
                      "icon": "", "hierarchical": False, "danger": "",
                      "forms": []}],
         "providers": [], "verbs": [],
         "topic_actions": [{"id": "mailbox-link", "label": "Mailbox",
                            "binding": "", "admin_only": True,
                            "fields": [{"name": "account", "label": "Casella",
                                        "required": True, "placeholder": "",
                                        "options": "mailboxes"}],
                            "adds": {"ingress": ["inbox:{address}"], "egress": []}}],
         "tools_card": None, "taint": []},
    ],
    "schemes": {"egress": ["tg"], "source": ["tg", "inbox"]},
    "problems": ["pack x: broken"],
    "ignored": ["y"],
}


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

    def connectors_registry(self):
        self.chiamate.append(("registry",))
        return REGISTRY

    def __getattr__(self, nome):
        if not nome.startswith("async_"):
            raise AttributeError(nome)
        sync = getattr(self, nome[len("async_"):])

        async def chiamata(*a, **k):
            return sync(*a, **k)

        return chiamata


class _Base(unittest.TestCase):
    ADMINS = {"davide"}
    HUMANS = {"davide", "giovanni", "mario"}

    def esegui(self, coro, chi="giovanni", cli=None):
        self.cli = cli or _Client()
        p = [patch.object(T, "topics_client", self.cli),
             patch.object(T, "_principal_from_request", lambda r: chi),
             patch.object(T.admin, "is_admin", lambda n: n in self.ADMINS),
             patch.object(T.admin, "is_human", lambda n: n in self.HUMANS)]
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


class PathTraversalTests(_Base):
    """Review of #521: `{connector}` and `{action}` used to go raw into the
    gateway URL. `%2E%2E` arrives here as `..`, `requests` strips dot segments,
    and `POST …/connectors/%2E%2E/mailbox-link` became
    `/internal/topics/SEAL-1/t/mailbox-link` — the specific route, with no
    `admin_only` in its state, ridden on the agent-server's trusted token."""

    CATTIVI = ("..", ".", "%2E%2E", "%2e%2e", "a/b", "../mail", "a%2Fb",
               "a%2fb", "telegram/..", "", "-x", "_x", "Telegram", "tele gram",
               "tg\\x", "x?y=1", "x#y")

    def test_a_bad_segment_is_a_400_and_nothing_is_forwarded(self):
        for cattivo in self.CATTIVI:
            for connector, action in ((cattivo, "telegram-link"),
                                      ("telegram", cattivo)):
                for chiamata in (
                        lambda: T.connector_action_status(
                            "SEAL-1", "acme", connector, action, _Req()),
                        lambda: T.connector_action_apply(
                            "SEAL-1", "acme", connector, action,
                            _Req({"action": "connect", "chat": "-100"}))):
                    with self.subTest(connector=connector, action=action):
                        with self.assertRaises(HTTPException) as e:
                            self.esegui(chiamata(), chi="davide")
                        self.assertEqual(e.exception.status_code, 400)
                        self.assertEqual(self.cli.chiamate, [])

    def test_the_client_refuses_too_and_quotes(self):
        """Second wall, in the function that builds the URL: whoever calls it
        tomorrow from another route inherits the check."""
        from . import topics_client as tc
        viste: list = []

        class _Http:
            def get(self, url, **k):
                viste.append(url)
                raise AssertionError("must not be reached")

            post = get

        with patch.object(tc, "_gw_http", _Http()), \
                patch.object(tc, "_headers", lambda: {}):
            for cattivo in ("..", "%2E%2E", "a/b", "a%2Fb"):
                with self.subTest(cattivo=cattivo):
                    with self.assertRaises(tc.TopicsClientError) as e:
                        tc.connector_action_status("SEAL-1", "t", cattivo, "x")
                    self.assertEqual(e.exception.status, 400)
                    with self.assertRaises(tc.TopicsClientError):
                        tc.connector_action_apply("SEAL-1", "t", "x", cattivo, {})
        self.assertEqual(viste, [])

    def test_through_the_real_router(self):
        """End to end on the HTTP layer: percent-encoded dots and slashes."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(T.router)
        self.cli = _Client()
        p = [patch.object(T, "topics_client", self.cli),
             patch.object(T, "_principal_from_request", lambda r: "davide"),
             patch.object(T.admin, "is_admin", lambda n: n in self.ADMINS),
             patch.object(T.admin, "is_human", lambda n: n in self.HUMANS)]
        for x in p:
            x.start()
        self.addCleanup(lambda: [x.stop() for x in reversed(p)])
        http = TestClient(app)
        for path in ("/api/topics/SEAL-1/acme/connectors/%2E%2E/mailbox-link",
                     "/api/topics/SEAL-1/acme/connectors/%2e%2e/mailbox-link",
                     "/api/topics/SEAL-1/acme/connectors/telegram/%2E%2E",
                     "/api/topics/SEAL-1/acme/connectors/a%2Fb/x",
                     "/api/topics/SEAL-1/acme/connectors/telegram/a%2F..%2F..%2Fx"):
            for metodo in ("get", "post"):
                with self.subTest(path=path, metodo=metodo):
                    r = getattr(http, metodo)(path, **(
                        {"json": {"action": "connect"}} if metodo == "post" else {}))
                    self.assertIn(r.status_code, (400, 404, 405))
        self.assertEqual(self.cli.chiamate, [])

    def test_a_good_segment_still_passes(self):
        res = self.esegui(T.connector_action_status(
            "SEAL-1", "acme", "openai-images", "image_link-2", _Req()))
        self.assertEqual(res["connector"], "openai-images")


class IdLengthTests(_Base):
    """clodia-platform#545 §3: il pattern non aveva un tetto di lunghezza.

    `^[a-z0-9][a-z0-9_-]*$` accetta cinquemila caratteri, e quei cinquemila
    caratteri arrivavano al gateway. Non è un bypass — il gateway non conosce
    quell'id e risponde 404 — ma è una richiesta che chiunque può far fare
    all'agent-server verso il gateway, su un collegamento che viaggia con il
    token FIDATO dell'agent-server, e la riga di log che lascia è sua.

    Il tetto non è un numero inventato: un id che il gateway possa davvero
    avere sta in 40 caratteri (`connectors/manifest._ID`, e i namespace in 32),
    quindi 64 lascia margine e non taglia niente di legittimo.
    """

    LUNGO = "a" * 65

    def test_an_overlong_id_is_a_400_before_the_gateway(self):
        for connector, action in ((self.LUNGO, "telegram-link"),
                                  ("telegram", self.LUNGO)):
            for chiamata in (
                    lambda: T.connector_action_status(
                        "SEAL-1", "acme", connector, action, _Req()),
                    lambda: T.connector_action_apply(
                        "SEAL-1", "acme", connector, action,
                        _Req({"action": "connect", "chat": "-100"}))):
                with self.subTest(connector=connector[:8], action=action[:8]):
                    with self.assertRaises(HTTPException) as e:
                        self.esegui(chiamata(), chi="davide")
                    self.assertEqual(e.exception.status_code, 400)
                    self.assertEqual(self.cli.chiamate, [])

    def test_the_longest_legitimate_id_still_passes(self):
        """Il taglio è SOPRA ciò che un manifest può dichiarare: se questo
        diventasse rosso, il tetto starebbe rifiutando un id vero."""
        al_limite = "a" * 64
        res = self.esegui(T.connector_action_status(
            "SEAL-1", "acme", al_limite, al_limite, _Req()))
        self.assertEqual(res["connector"], al_limite)

    def test_the_client_refuses_it_too(self):
        """Seconda parete, dove l'URL viene costruita: la rotta non è l'unico
        chiamante possibile."""
        from . import topics_client as tc
        viste: list = []

        class _Http:
            def get(self, url, **k):
                viste.append(url)
                raise AssertionError("must not be reached")

            post = get

        with patch.object(tc, "_gw_http", _Http()), \
                patch.object(tc, "_headers", lambda: {}):
            with self.assertRaises(tc.TopicsClientError) as e:
                tc.connector_action_status("SEAL-1", "t", self.LUNGO, "x")
            self.assertEqual(e.exception.status, 400)
            with self.assertRaises(tc.TopicsClientError):
                tc.connector_action_apply("SEAL-1", "t", "x", self.LUNGO, {})
        self.assertEqual(viste, [])

    def test_the_two_walls_use_the_same_pattern(self):
        """Le due copie esistono apposta (la rotta non deve dipendere dal
        client), ma devono dire la stessa cosa: una divergenza non si vede,
        perché ciascuna delle due da sola sembra giusta."""
        from . import topics_client as tc
        self.assertEqual(T._CONNECTOR_SEGMENT.pattern,
                         tc.CONNECTOR_SEGMENT.pattern)

    def test_the_refusal_does_not_echo_the_whole_id(self):
        """Un messaggio che ricopia cinquemila caratteri sposta il costo dal
        gateway al log di chi rifiuta."""
        from . import topics_client as tc
        with self.assertRaises(tc.TopicsClientError) as e:
            tc.connector_action_status("SEAL-1", "t", "a" * 5000, "x")
        self.assertLess(len(str(e.exception)), 200)


class AnAgentRuntimeTokenIsNotAPersonTests(_Base):
    """Review of #521, reopening #539 §2: a topic created without an explicit
    owner is owned by `clodia`, whose runtime token lives in every spawn. The
    generic routes must want a PERSON, like `tg-link` and `local-folder`."""

    def _cli_agente(self):
        cli = _Client()
        cli.open_topic = lambda tier, name: {"meta": {
            "tier": "SEAL-1", "owner": "clodia",
            "participants": {"clodia": "owner"}}}
        return cli

    def test_the_agent_owner_cannot_bind_a_chat(self):
        with self.assertRaises(HTTPException) as e:
            self.esegui(T.connector_action_apply(
                "SEAL-1", "ops", "telegram", "telegram-link",
                _Req({"action": "connect", "chat": "-100777"})),
                chi="clodia", cli=self._cli_agente())
        self.assertEqual(e.exception.status_code, 403)
        self.assertEqual(self.cli.chiamate, [])

    def test_nor_read_the_action_state(self):
        with self.assertRaises(HTTPException) as e:
            self.esegui(T.connector_action_status(
                "SEAL-1", "ops", "telegram", "telegram-link", _Req()),
                chi="clodia", cli=self._cli_agente())
        self.assertEqual(e.exception.status_code, 403)
        self.assertEqual(self.cli.chiamate, [])

    def test_nor_list_the_topic_connectors(self):
        with self.assertRaises(HTTPException) as e:
            self.esegui(T.topic_connectors("SEAL-1", "ops", _Req()),
                        chi="clodia", cli=self._cli_agente())
        self.assertEqual(e.exception.status_code, 403)
        self.assertEqual(self.cli.chiamate, [])

    def test_the_human_owner_passes(self):
        res = self.esegui(T.connector_action_apply(
            "SEAL-1", "acme", "telegram", "telegram-link",
            _Req({"action": "connect", "chat": "-100"})), chi="giovanni")
        self.assertTrue(res["connected"])


class TopicConnectorListingTests(_Base):
    """Review of clodia-web#242: the topic page drew its buttons from
    `GET /tools/connectors`, which the gateway serves to admins only. A
    non-admin owner got a silent 401 and lost the Telegram/Drive buttons. This
    listing is owner-scoped and carries only what the topic page draws."""

    def test_the_human_owner_gets_the_topic_actions(self):
        res = self.esegui(T.topic_connectors("SEAL-1", "acme", _Req()),
                          chi="giovanni")
        self.assertEqual(res["tier"], "SEAL-1")
        ids = [c["id"] for c in res["connectors"]]
        self.assertIn("telegram", ids)
        tg = next(c for c in res["connectors"] if c["id"] == "telegram")
        self.assertEqual([a["id"] for a in tg["topic_actions"]], ["telegram-link"])
        self.assertEqual(tg["topic_actions"][0]["fields"][0]["name"], "chat")
        self.assertEqual(tg["schemes"][0]["forms"][0]["example"], "-1001234567890")
        self.assertEqual(tg["verbs"], [{"verb": "telegram.send", "dtype": "telegram"}])
        self.assertEqual(tg["tools_card"], {"icon": "telegram"})

    def test_no_vault_or_admin_data_leaks(self):
        res = self.esegui(T.topic_connectors("SEAL-1", "acme", _Req()),
                          chi="giovanni")
        testo = repr(res)
        for vietato in ("vault_entry", "telegram_bot_token", "providers",
                        "problems", "ignored", "pack x: broken", "docs_url",
                        "status_verb", "blurb", "taint", "adds", "tg:{chat}"):
            with self.subTest(vietato=vietato):
                self.assertNotIn(vietato, testo)

    def test_admin_only_actions_are_hidden_from_a_non_admin_owner(self):
        res = self.esegui(T.topic_connectors("SEAL-1", "acme", _Req()),
                          chi="giovanni")
        azioni = [a["id"] for c in res["connectors"] for a in c["topic_actions"]]
        self.assertNotIn("mailbox-link", azioni)

    def test_an_admin_sees_them(self):
        res = self.esegui(T.topic_connectors("SEAL-1", "acme", _Req()),
                          chi="davide")
        azioni = [a["id"] for c in res["connectors"] for a in c["topic_actions"]]
        self.assertIn("mailbox-link", azioni)
        mail = next(c for c in res["connectors"] if c["id"] == "mail")
        self.assertTrue(mail["topic_actions"][0]["admin_only"])
        self.assertEqual(mail["topic_actions"][0]["fields"][0]["options"], "mailboxes")

    def test_a_participant_who_is_not_the_owner_is_refused(self):
        with self.assertRaises(HTTPException) as e:
            self.esegui(T.topic_connectors("SEAL-1", "acme", _Req()), chi="mario")
        self.assertEqual(e.exception.status_code, 403)
        self.assertEqual(self.cli.chiamate, [])

    def test_the_gateway_is_asked_with_the_agent_server_token(self):
        """Not the user's session: the gateway serves the registry to admins
        and to the agent-server (`role: agent-server`), and this route has
        already decided who may see the reduced view."""
        from . import topics_client as tc
        from ..colony import pki
        viste: list = []

        class _R:
            status_code = 200

            def json(self):
                return REGISTRY

        class _Http:
            def get(self, url, headers=None, **k):
                viste.append((url, headers))
                return _R()

        coniati: list = []

        def _mint(principal, ttl_seconds=None, role=None, **k):
            coniati.append(role)
            return "tok"

        with patch.object(tc, "_gw_http", _Http()), \
                patch.object(tc.pki, "mint_session_token", _mint), \
                patch.object(tc, "_base",
                             lambda: "http://gw:7849/internal/topics"):
            self.assertEqual(tc.connectors_registry(), REGISTRY)
        self.assertEqual(viste[0][0], "http://gw:7849/internal/connectors")
        self.assertEqual(viste[0][1], {"Authorization": "Bearer tok"})
        self.assertEqual(coniati, [pki.ROLE_AGENT_SERVER])

    def test_a_gateway_refusal_keeps_its_status(self):
        cli = _Client()

        def _rotto():
            raise _Client.TopicsClientError("x", status=401, detail="unauthorized")

        cli.connectors_registry = _rotto
        with self.assertRaises(HTTPException) as e:
            self.esegui(T.topic_connectors("SEAL-1", "acme", _Req()), cli=cli)
        self.assertEqual(e.exception.status_code, 401)


class ClientShapeTests(unittest.TestCase):
    def test_the_async_wrappers_exist(self):
        """Un handler `async def` chiama SEMPRE il lato async: la versione
        sincrona dentro il loop bloccherebbe il processo, e c'è un guardrail
        sull'AST che lo vieta."""
        from . import topics_client
        for nome in ("async_connector_action_status", "async_connector_action_apply",
                     "async_connectors_registry"):
            self.assertTrue(callable(getattr(topics_client, nome, None)), nome)


if __name__ == "__main__":
    unittest.main()
