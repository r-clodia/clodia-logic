"""clodia-platform#443 — human oversight and anomalies, with their cause.

On 29 Sep 05:35 the log said «sessione ripristinata e pronta dopo fallimento
turno» with no cause: it had followed a credential refresh, not a failure.
A recovery now states its cause on the trail; a human interrupt says who
stopped whom; a router override says who corrected which choice.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from . import audit_events
from .sdk_runtime import test_session_recovery as tsr


class BuilderTests(unittest.TestCase):
    def test_interrupt_names_the_human_and_the_targets(self) -> None:
        ev = audit_events.interrupt("SEAL-2", "t", "davide", ["worker-221"], ["worker-221"])
        self.assertEqual((ev["type"], ev["actor"]["id"]), ("turn.interrupt", "davide"))
        self.assertEqual(ev["decision"]["targets"], ["worker-221"])
        everyone = audit_events.interrupt("SEAL-2", "t", "davide", None, 2)
        self.assertEqual(everyone["decision"]["targets"], ["*"])

    def test_override_names_who_corrected_what(self) -> None:
        ev = audit_events.override("SEAL-1", "t", "davide", ["segretario-3"], "avvocato", "overruled")
        self.assertEqual(ev["type"], "route.override")
        self.assertEqual(ev["decision"], {"misrouted": ["segretario-3"], "chosen": "avvocato"})


class RecoveryCauseTests(unittest.IsolatedAsyncioTestCase):
    async def _recover(self, cause=None):
        sess = tsr._make_session()
        sess._opts_kwargs = {}
        sess._client_ctx = None
        sess._refresh_provider_env = lambda: False
        sess._open_client = AsyncMock()
        if cause:
            sess._recover_cause = cause
        seen = []
        with patch.object(audit_events, "_bg", lambda ev: seen.append(ev)):
            await sess._recover_session()
        return seen[0]

    async def test_a_credential_refresh_is_not_a_failure(self) -> None:
        ev = await self._recover("credential_refresh")
        self.assertEqual((ev["type"], ev["result"]["cause"], ev["result"]["ok"]),
                         ("turn.recover", "credential_refresh", True))
        self.assertEqual(ev["scope"], {"tier": "SEAL-1", "topic": "test"})

    async def test_by_default_the_cause_is_a_turn_failure(self) -> None:
        self.assertEqual((await self._recover())["result"]["cause"], "turn_failure")


if __name__ == "__main__":
    unittest.main()
