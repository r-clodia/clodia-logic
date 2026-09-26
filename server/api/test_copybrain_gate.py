"""Il gate `copybrain:<seed>` lato agent-server (clodia-platform#393).

Il consenso vale per lo spawn che lo chiede, fino alla sua fine:

- l'approvazione non si «ricorda» per la stanza né per l'istanza — varrebbe per
  gli spawn futuri, cioè per il seed;
- senza spawn non si approva: il consenso non sarebbe scopabile;
- la capability chiesta dura quanto uno spawn (tetto lungo), non 10 minuti;
- a fine spawn i prestiti vengono ritirati, e un gateway irraggiungibile non
  impedisce la chiusura.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from . import gate as G
from .test_gate_duplicate_pending import _Req, _Risposta


def _approva(body, *, pendenti):
    coniate = []

    def _gw(metodo, path, principal, corpo=None):
        if path == "/pending":
            return _Risposta(200, {"requests": pendenti})
        return _Risposta(200, {"ok": True})

    def _mint(agent, instance, minutes, **k):
        coniate.append((agent, instance, minutes, k.get("cap")))
        return {"token": "ccap1…", "jti": "j"}

    with patch.object(G, "_gw", _gw), \
            patch.object(G, "_is_scope_owner", lambda p, s: True), \
            patch.object(G.admin, "is_admin", lambda p: True), \
            patch.object(G, "_principal_from_request", lambda r: "davide"), \
            patch.object(G.pki, "mint_capability", _mint), \
            patch.object(G, "_post_outcome", lambda *a, **k: None):
        r = asyncio.run(G.approve(_Req(body)))
    return r.status_code, json.loads(bytes(r.body).decode()), coniate


_PENDENTE = [{"agent": "clodia", "instance": "clodia-3", "verb": "copybrain:commercialista",
              "class": "walls", "chat": "chan:SEAL-1:acme:clodia"}]
_BODY = {"agent": "clodia", "instance": "clodia-3", "verb": "copybrain:commercialista"}


class CopybrainApproveTests(unittest.TestCase):
    def test_the_consent_lasts_a_spawn_not_ten_minutes(self):
        code, corpo, coniate = _approva(dict(_BODY), pendenti=_PENDENTE)
        self.assertEqual(200, code, corpo)
        self.assertEqual([("clodia", "clodia-3", G.COPYBRAIN_MINUTES,
                           "gate:copybrain:commercialista")], coniate)

    def test_it_is_never_remembered(self):
        for ricorda in ("topic", "global"):
            with self.subTest(ricorda=ricorda):
                code, _corpo, coniate = _approva({**_BODY, "remember": ricorda},
                                                 pendenti=_PENDENTE)
                self.assertEqual(400, code)
                self.assertEqual([], coniate)

    def test_without_a_spawn_it_is_not_approved(self):
        code, _corpo, coniate = _approva({**_BODY, "instance": "-"}, pendenti=_PENDENTE)
        self.assertEqual(400, code)
        self.assertEqual([], coniate)

    def test_an_ordinary_gate_keeps_its_ten_minutes(self):
        pend = [{"agent": "sysadmin", "instance": "-", "verb": "egress.allow",
                 "class": "walls", "chat": "chan:SEAL-1:acme:sysadmin"}]
        _code, _c, coniate = _approva(
            {"agent": "sysadmin", "instance": "-", "verb": "egress.allow"}, pendenti=pend)
        self.assertEqual(10, coniate[0][2])


class ReleaseAtSpawnEndTests(unittest.TestCase):
    def test_release_calls_the_gateway_for_that_spawn(self):
        chiamate = []

        def _gw(metodo, path, principal, corpo=None):
            chiamate.append((metodo, path, corpo))
            return _Risposta(200, {"ok": True, "revoked": ["copybrain:commercialista"]})

        with patch.object(G, "_gw", _gw):
            self.assertEqual(["copybrain:commercialista"],
                             G.release_spawn_loans("clodia", "clodia-3"))
        self.assertEqual([("POST", "/revoke_instance",
                           {"agent": "clodia", "instance": "clodia-3"})], chiamate)

    def test_an_unreachable_gateway_does_not_raise(self):
        def _gw(*a, **k):
            raise ConnectionError("gateway giù")

        with patch.object(G, "_gw", _gw):
            self.assertEqual([], G.release_spawn_loans("clodia", "clodia-3"))

    def test_no_spawn_no_call(self):
        with patch.object(G, "_gw", side_effect=AssertionError("non va chiamato")):
            self.assertEqual([], G.release_spawn_loans("clodia", "-"))


class LocalCapabilityCeilingTests(unittest.TestCase):
    def test_the_local_fallback_applies_the_same_ceiling(self):
        import inspect
        from ..colony import pki
        src = inspect.getsource(pki.mint_capability)
        self.assertIn('startswith("gate:copybrain:")', src)
        self.assertIn("24 * 60", src)


if __name__ == "__main__":
    unittest.main()
