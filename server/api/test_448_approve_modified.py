"""clodia-platform#448 — the approve route carries the approver's corrections.

The gateway validates them against the verb's editable fields and re-judges
the corrected call; this route passes them through, refuses what cannot be a
correction, and refuses to REMEMBER a correction (it answers this call only).

A gateway without gate-modify (clodia-tools #339) would silently grant the
ORIGINAL call. So a correction is refused before minting when the gateway does
not advertise `editable` on the pending request, and a grant that does not
confirm the correction is reported as a failure, never as an approval.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from . import gate as G
from .test_gate_duplicate_pending import _Req, _Risposta

PENDING = [{"agent": "clodia", "instance": "-", "verb": "email.send", "class": "outward",
            "chat": "chan:SEAL-1:acme:clodia", "editable": {"to": "sbagliato@x.it"}}]
OLD_GATEWAY_PENDING = [{k: v for k, v in PENDING[0].items() if k != "editable"}]
BASE = {"agent": "clodia", "instance": "-", "verb": "email.send"}


def _echo(corpo):
    """A gate-modify gateway: the grant echoes the validated correction."""
    return {"ok": True, "agent": "clodia", "instance": "-", "verb": "email.send",
            "expires_in_s": 600, **({"modified": corpo["modified"]}
                                    if "modified" in (corpo or {}) else {})}


def _chiama(body, pendenti, grant=_echo):
    chiamate, posted = [], []

    def _gw(metodo, path, principal, corpo=None):
        chiamate.append((metodo, path, corpo))
        if path == "/pending":
            return _Risposta(200, {"requests": pendenti})
        if path == "/grant":
            return _Risposta(200, grant(corpo))
        return _Risposta(200, {"ok": True})

    with patch.object(G, "_gw", _gw), \
            patch.object(G, "_is_scope_owner", lambda p, s: tuple(s) == ("SEAL-1", "acme")), \
            patch.object(G.admin, "is_admin", lambda p: False), \
            patch.object(G, "_principal_from_request", lambda r: "davide"), \
            patch.object(G.pki, "mint_capability",
                         lambda *a, **k: {"token": "ccap1…", "jti": "j"}), \
            patch.object(G, "_post_outcome", lambda *a, **k: posted.append(a[-1])):
        risposta = asyncio.run(G.approve(_Req(body)))
    return (risposta.status_code, json.loads(bytes(risposta.body).decode()),
            chiamate, posted)


def grants(calls):
    return [c[2] for c in calls if c[1] == "/grant"]


class ApproveModifiedTests(unittest.TestCase):
    def test_the_corrections_reach_the_gateway(self) -> None:
        code, body, calls, posted = _chiama({**BASE, "arguments": {"to": "giusto@x.it"}},
                                            PENDING)
        self.assertEqual(code, 200, body)
        self.assertEqual(grants(calls)[0]["modified"], {"to": "giusto@x.it"})
        self.assertTrue(posted and posted[0].startswith("🔓"))

    def test_without_corrections_the_grant_is_as_before(self) -> None:
        code, _body, calls, _p = _chiama(BASE, OLD_GATEWAY_PENDING)
        self.assertEqual(code, 200)
        self.assertNotIn("modified", grants(calls)[0])

    def test_a_correction_is_not_an_object(self) -> None:
        code, _body, calls, _p = _chiama({**BASE, "arguments": "to=x"}, PENDING)
        self.assertEqual(code, 400)
        self.assertEqual(grants(calls), [])

    def test_a_correction_is_not_remembered(self) -> None:
        code, _body, calls, _p = _chiama({**BASE, "arguments": {"to": "x@x.it"},
                                          "remember": "topic"}, PENDING)
        self.assertEqual(code, 400)
        self.assertEqual(grants(calls), [])


class GatewayWithoutGateModifyTests(unittest.TestCase):
    def test_refused_before_minting_when_the_gateway_lists_no_editable(self) -> None:
        minted = []
        with patch.object(G.pki, "mint_capability",
                          lambda *a, **k: minted.append(a) or {"token": "t", "jti": "j"}):
            code, body, calls, _p = _chiama({**BASE, "arguments": {"to": "giusto@x.it"}},
                                            OLD_GATEWAY_PENDING)
        self.assertEqual(code, 409, body)
        self.assertEqual(body["error"], "correction_unsupported")
        self.assertIn("#339", body["detail"])
        self.assertEqual(grants(calls), [])

    def test_a_grant_that_does_not_confirm_the_correction_fails_loudly(self) -> None:
        code, body, calls, posted = _chiama(
            {**BASE, "arguments": {"to": "giusto@x.it"}, "chat": "chan:SEAL-1:acme:clodia"},
            PENDING, grant=lambda corpo: {"ok": True, "agent": "clodia", "verb": "email.send"})
        self.assertEqual(len(grants(calls)), 1)
        self.assertEqual(code, 502, body)
        self.assertEqual(body["error"], "correction_not_confirmed")
        self.assertEqual(len(posted), 1)
        self.assertIn("NON ha confermato", posted[0])
        self.assertFalse(posted[0].startswith("🔓"))

    def test_a_grant_that_confirms_other_values_is_not_a_confirmation(self) -> None:
        code, body, _c, _p = _chiama(
            {**BASE, "arguments": {"to": "giusto@x.it"}}, PENDING,
            grant=lambda corpo: {"ok": True, "modified": {"to": "altro@x.it"}})
        self.assertEqual((code, body["error"]), (502, "correction_not_confirmed"))

    def test_modified_fields_is_an_accepted_confirmation(self) -> None:
        code, body, _c, _p = _chiama(
            {**BASE, "arguments": {"to": "giusto@x.it"}}, PENDING,
            grant=lambda corpo: {"ok": True, "modified_fields": ["to"]})
        self.assertEqual(code, 200, body)


if __name__ == "__main__":
    unittest.main()
