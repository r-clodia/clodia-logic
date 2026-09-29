"""clodia-platform#448 — the approve route carries the approver's corrections.

The gateway validates them against the verb's editable fields and re-judges
the corrected call; this route passes them through, refuses what cannot be a
correction, and refuses to REMEMBER a correction (it answers this call only).
"""
from __future__ import annotations

import unittest

from .test_gate_duplicate_pending import _chiama

PENDING = [{"agent": "clodia", "instance": "-", "verb": "email.send", "class": "outward",
            "chat": "chan:SEAL-1:acme:clodia"}]
BASE = {"agent": "clodia", "instance": "-", "verb": "email.send"}


class ApproveModifiedTests(unittest.TestCase):
    def grants(self, calls):
        return [c[2] for c in calls if c[1] == "/grant"]

    def test_the_corrections_reach_the_gateway(self) -> None:
        code, _body, calls = _chiama("davide", {**BASE, "arguments": {"to": "giusto@x.it"}},
                                     PENDING)
        self.assertEqual(code, 200)
        self.assertEqual(self.grants(calls)[0]["modified"], {"to": "giusto@x.it"})

    def test_without_corrections_the_grant_is_as_before(self) -> None:
        _code, _body, calls = _chiama("davide", BASE, PENDING)
        self.assertNotIn("modified", self.grants(calls)[0])

    def test_a_correction_is_not_an_object(self) -> None:
        code, _body, calls = _chiama("davide", {**BASE, "arguments": "to=x"}, PENDING)
        self.assertEqual(code, 400)
        self.assertEqual(self.grants(calls), [])

    def test_a_correction_is_not_remembered(self) -> None:
        code, _body, calls = _chiama("davide", {**BASE, "arguments": {"to": "x@x.it"},
                                                "remember": "topic"}, PENDING)
        self.assertEqual(code, 400)
        self.assertEqual(self.grants(calls), [])


if __name__ == "__main__":
    unittest.main()
