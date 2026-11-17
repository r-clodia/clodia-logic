"""Fin dove vale un sì: stavolta, questa stanza, tutta l'istanza.

Un gate di uscita è la domanda «questa destinazione va bene?». Poterla
rispondere solo «per stavolta» significa riproporla identica ogni volta — e una
domanda che torna sempre uguale si finisce per approvarla per riflesso, cioè il
gate smette di essere un controllo e diventa un rumore da spegnere. Ricordare la
risposta è ciò che lo tiene significativo: chiede quando c'è davvero qualcosa di
nuovo da decidere.

**Le tre portate non hanno lo stesso titolare**, ed è il punto di questi test.
Chi possiede la stanza decide per la stanza. Per l'intera istanza decide chi
possiede l'istanza: «approva ovunque» scrive in una lista che vale anche nelle
stanze di cui chi approva non sa nulla, e un owner che la usasse deciderebbe al
posto di altri owner.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from . import gate as G


PENDING = {"requests": [{"agent": "sysadmin", "instance": "-",
                         "verb": "egress:github:https://github.com/r-clodia/x",
                         "class": "outward", "chat": "chan:SEAL-1:acme:sysadmin"}]}


class _Risposta:
    def __init__(self, code=200, corpo=None):
        self.status_code = code
        self._c = corpo if corpo is not None else {"ok": True}
        self.text = json.dumps(self._c)

    def json(self):
        return self._c


class _Req:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


def _chiama(chi, body, *, admin_ok=False, owner_ok=True, gw=None):
    chiamate = []

    def _gw(metodo, path, principal, corpo=None):
        chiamate.append((metodo, path, corpo))
        if path == "/pending":
            return _Risposta(200, PENDING)
        if path == "/allow":
            return _Risposta(200, {"remembered": True, "uri": "…"})
        return _Risposta(200, {"ok": True})

    with patch.object(G, "_gw", gw or _gw), \
         patch.object(G, "_principal_from_request", lambda r: chi), \
         patch.object(G.admin, "is_admin", lambda n: admin_ok), \
         patch.object(G, "_is_scope_owner", lambda p, s: owner_ok), \
         patch.object(G, "_post_outcome", lambda *a, **k: None), \
         patch.object(G.pki, "mint_capability",
                      lambda *a, **k: {"token": "ccap1.x", "jti": "j"}):
        r = asyncio.run(G.approve(_Req(body)))
    return r, chiamate


BASE = {"agent": "sysadmin", "instance": "-",
        "verb": "egress:github:https://github.com/r-clodia/x"}


class WhoMayRememberTests(unittest.TestCase):
    def test_the_scope_owner_may_approve_once(self):
        r, _ = _chiama("davide", {**BASE, "remember": "once"})
        self.assertEqual(r.status_code, 200)

    def test_the_scope_owner_may_remember_for_the_room(self):
        r, chiamate = _chiama("davide", {**BASE, "remember": "topic"})
        self.assertEqual(r.status_code, 200)
        allow = [c for c in chiamate if c[1] == "/allow"]
        self.assertEqual(len(allow), 1)
        self.assertEqual(allow[0][2]["scope"], "SEAL-1/acme")

    def test_the_scope_owner_may_NOT_remember_for_the_whole_instance(self):
        """Il caso che dà senso alla separazione: la lista globale vale anche
        nelle stanze che non sono sue."""
        r, chiamate = _chiama("davide", {**BASE, "remember": "global"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual([c for c in chiamate if c[1] == "/allow"], [])

    def test_an_admin_may(self):
        r, chiamate = _chiama("davide", {**BASE, "remember": "global"}, admin_ok=True)
        self.assertEqual(r.status_code, 200)
        allow = [c for c in chiamate if c[1] == "/allow"]
        self.assertEqual(allow[0][2]["scope"], "")   # vuoto = lista globale

    def test_someone_without_standing_is_refused_before_anything_else(self):
        """Il titolo sul gate viene prima della portata: chi non può approvare
        non può nemmeno ricordare."""
        r, chiamate = _chiama("estraneo", {**BASE, "remember": "topic"},
                              owner_ok=False)
        self.assertEqual(r.status_code, 403)
        self.assertEqual([c for c in chiamate if c[1] == "/allow"], [])

    def test_an_unknown_scope_word_is_refused(self):
        r, _ = _chiama("davide", {**BASE, "remember": "per-sempre-forse"})
        self.assertEqual(r.status_code, 400)


class TheRoomComesFromTheGatewayTests(unittest.TestCase):
    """In quale stanza ricordare lo dice il gateway, non il corpo della
    richiesta: chiederlo a chi approva significherebbe fidarsi della sua parola
    su dove si trovava l'azione — e qui quella parola diventa una voce in una
    whitelist permanente."""

    def test_the_body_cannot_choose_the_room(self):
        r, chiamate = _chiama("davide", {**BASE, "remember": "topic",
                                         "chat": "chan:SEAL-3:altrui:x"})
        allow = [c for c in chiamate if c[1] == "/allow"]
        self.assertEqual(allow[0][2]["scope"], "SEAL-1/acme")

    def test_without_a_room_it_says_so_instead_of_guessing(self):
        senza = {"requests": [{**PENDING["requests"][0], "chat": None,
                               "class": None}]}

        def _gw(metodo, path, principal, corpo=None):
            if path == "/pending":
                return _Risposta(200, senza)
            return _Risposta(200, {"ok": True})

        r, _ = _chiama("davide", {**BASE, "remember": "topic"},
                       admin_ok=True, gw=_gw)
        corpo = json.loads(bytes(r.body).decode())
        self.assertFalse(corpo["memory"]["remembered"])
        self.assertIn("stanza", corpo["memory"]["error"])


class ApprovalSurvivesAFailedMemoryTests(unittest.TestCase):
    """Se scrivere la lista fallisce, l'approvazione resta valida: l'agente in
    attesa non deve restare bloccato per un difetto della memoria. Ma la
    differenza va DETTA, o la stessa domanda torna domani e chi la rivede pensa
    che il gate sia rotto."""

    def test_the_agent_proceeds_and_the_answer_says_it_was_not_remembered(self):
        def _gw(metodo, path, principal, corpo=None):
            if path == "/pending":
                return _Risposta(200, PENDING)
            if path == "/allow":
                return _Risposta(400, {"error": "schema non ammesso"})
            return _Risposta(200, {"ok": True})

        r, _ = _chiama("davide", {**BASE, "remember": "topic"}, gw=_gw)
        self.assertEqual(r.status_code, 200)     # il gate È stato approvato
        corpo = json.loads(bytes(r.body).decode())
        self.assertFalse(corpo["memory"]["remembered"])


if __name__ == "__main__":
    unittest.main()


class MatrixTests(unittest.TestCase):
    """clodia-platform#693: CHI (spawn · seed · tutti) × DOVE (stanza · istanza).
    Il gateway scrive la regola; qui si decide chi ha titolo e cosa chiedere."""

    AUTHZ = "authz:web.post|https://api.example.com/"

    def _gw(self, verb, remember_ok=True):
        pend = {"requests": [{**PENDING["requests"][0], "verb": verb,
                              "agent": "officer", "instance": "officer-28"}]}

        self.calls = []

        def gw(metodo, path, principal, corpo=None):
            self.calls.append((metodo, path, corpo))
            if path == "/pending":
                return _Risposta(200, pend)
            if path == "/remember":
                return (_Risposta(200, {"remembered": True, "kind": "rule"}) if remember_ok
                        else _Risposta(400, {"remembered": False,
                                             "error": "si decide a livello globale"}))
            if path == "/allow":
                return _Risposta(200, {"remembered": True, "uri": "…"})
            return _Risposta(200, {"ok": True})
        return gw

    def body(self, verb, **kw):
        return {"agent": "officer", "instance": "officer-28", "verb": verb, **kw}

    def test_this_seed_in_this_room_writes_a_rule_for_the_seed(self):
        r, _ = _chiama("davide", self.body(self.AUTHZ, who="seed", where="chat"),
                        gw=self._gw(self.AUTHZ))
        self.assertEqual(r.status_code, 200)
        (rem,) = [c for c in self.calls if c[1] == "/remember"]
        self.assertEqual((rem[2]["agent"], rem[2]["scope"], rem[2]["verb"]),
                         ("officer", "SEAL-1/acme", self.AUTHZ))
        self.assertTrue(json.loads(bytes(r.body).decode())["memory"]["remembered"])

    def test_every_agent_on_an_authz_gate_is_a_rule_for_star(self):
        r, _ = _chiama("davide", self.body(self.AUTHZ, who="all", where="chat"),
                        gw=self._gw(self.AUTHZ))
        (rem,) = [c for c in self.calls if c[1] == "/remember"]
        self.assertEqual(rem[2]["agent"], "*")
        self.assertEqual([c for c in self.calls if c[1] == "/allow"], [])

    def test_every_agent_on_a_destination_keeps_the_lists(self):
        verb = BASE["verb"]
        r, _ = _chiama("davide", self.body(verb, who="all", where="chat"),
                        gw=self._gw(verb))
        self.assertEqual(len([c for c in self.calls if c[1] == "/allow"]), 1)
        self.assertEqual([c for c in self.calls if c[1] == "/remember"], [])

    def test_the_instance_column_is_an_admin_s(self):
        r, _ = _chiama("davide", self.body(self.AUTHZ, who="seed", where="global"),
                        gw=self._gw(self.AUTHZ))
        self.assertEqual(r.status_code, 403)
        self.assertEqual([c for c in self.calls if c[1] in ("/remember", "/grant")], [])
        r, _ = _chiama("davide", self.body(self.AUTHZ, who="seed", where="global"),
                        admin_ok=True, gw=self._gw(self.AUTHZ))
        (rem,) = [c for c in self.calls if c[1] == "/remember"]
        self.assertEqual(rem[2]["scope"], "")

    def test_this_spawn_is_today_s_consent_only(self):
        r, _ = _chiama("davide", self.body(self.AUTHZ, who="spawn"),
                        gw=self._gw(self.AUTHZ))
        self.assertEqual(r.status_code, 200)
        self.assertEqual([c for c in self.calls if c[1] in ("/remember", "/allow")], [])

    def test_a_cell_the_gate_does_not_have_is_said_and_the_call_proceeds(self):
        r, _ = _chiama("davide", self.body(self.AUTHZ, who="seed", where="chat"),
                       gw=self._gw(self.AUTHZ, remember_ok=False))
        self.assertEqual(r.status_code, 200)
        mem = json.loads(bytes(r.body).decode())["memory"]
        self.assertFalse(mem["remembered"])
        self.assertIn("globale", mem["error"])

    def test_unknown_words_are_refused(self):
        for body in (self.body(self.AUTHZ, who="qualcuno", where="chat"),
                     self.body(self.AUTHZ, who="seed", where="ovunque"),
                     self.body(self.AUTHZ, who="seed")):
            with self.subTest(body=body):
                r, _ = _chiama("davide", body, gw=self._gw(self.AUTHZ))
                self.assertEqual(r.status_code, 400)
