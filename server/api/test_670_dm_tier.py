"""clodia-platform#670 — the DM's owner sets the DM's tier (owner, 11 Nov 2026).

- a new DM is SEAL-0 (the default), and reopening returns the DM at the tier
  its owner set, instead of re-creating it at SEAL-0;
- re-classifying a DM follows the topic route, plus one rule: the tier cannot
  exceed the DM owner's clearance (preview and apply);
- an agent participant keeps acting only with clearance ≥ the tier
  (`_tier_impact` reports it, as for topics); lowering below the DM's imports
  is the gateway's `check_retier` (clodia-tools tests).
"""
from __future__ import annotations

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from . import channels as C, topics as T

DM = {"tier": "SEAL-0", "kind": "dm", "type": "dm", "owner": "davide",
      "participants": ["davide", "clodia"], "contact_agent": "clodia"}


class _Req:
    def __init__(self, body=None):
        self._b = body

    async def json(self):
        return self._b


class DmCreateTests(unittest.TestCase):
    def _create(self, existing: dict[str, dict]):
        async def open_topic(t, n):
            return existing.get(t)
        create = AsyncMock(return_value={"title": "x"})
        with patch.object(C, "_principal_from_request", lambda r: "davide"), \
                patch.object(C.topics_client, "async_open_topic", open_topic), \
                patch.object(C.topics_client, "async_create_topic", create):
            req = types.SimpleNamespace(json=AsyncMock(return_value={"with": "clodia"}))
            return asyncio.run(C.dm_create(req)), create

    def test_a_new_dm_is_seal0(self):
        out, create = self._create({})
        self.assertEqual(out["tier"], "SEAL-0")
        create.assert_awaited_once()
        self.assertEqual(create.await_args.args[:2], ("SEAL-0", "dm-clodia--davide"))

    def test_reopening_keeps_the_tier_the_owner_set(self):
        out, create = self._create({"SEAL-2": {"meta": dict(DM, tier="SEAL-2")}})
        self.assertEqual((out["tier"], out["name"]), ("SEAL-2", "dm-clodia--davide"))
        create.assert_not_awaited()


class DmRetierTests(unittest.TestCase):
    def _run(self, coro_fn, owner_clearance="SEAL-2", who="davide", meta=None):
        from . import channels
        spec = types.SimpleNamespace(name="clodia", type="bot", seal=1)
        p = [patch.object(T, "_principal_from_request", lambda r: who),
             patch.object(T.admin, "is_admin", lambda n: n == "admin"),
             patch.object(channels, "_is_human_principal", lambda n: n in ("davide", "admin")),
             patch.object(T.topics_client, "open_topic", lambda t, n: {"meta": dict(meta or DM)}),
             patch.object(T, "_principal_clearance", lambda n: owner_clearance),
             patch.object(channels, "_eligibility",
                          lambda s, tier: {"eligible": int(tier[-1]) <= s.seal}),
             patch("server.api.agent_registry.registry.get_by_name",
                   lambda n: spec if n == "clodia" else None)]
        for x in p:
            x.start()
        try:
            return asyncio.run(coro_fn())
        finally:
            for x in reversed(p):
                x.stop()

    def _apply(self, tier, **kw):
        gw = AsyncMock(return_value={"from": "SEAL-0", "to": tier, "name": "dm-clodia--davide"})
        body = {"tier": tier, "reason": "CRM nel DM", "accept_responsibility": True}
        with patch.object(T.topics_client, "async_set_topic_tier", gw), \
                patch("server.scheduler.db.retier_topic_jobs", return_value=[]), \
                patch("server.sdk_runtime.session.manager") as m:
            m.list.return_value = []
            res = self._run(lambda: T.topic_set_tier("SEAL-0", "dm-clodia--davide",
                                                     _Req(body)), **kw)
        return res, gw

    def test_the_owner_raises_the_dm_within_their_clearance(self):
        res, gw = self._apply("SEAL-2")
        self.assertEqual(res["to"], "SEAL-2")
        gw.assert_awaited_once()

    def test_the_dm_cannot_exceed_the_owners_clearance(self):
        with self.assertRaises(HTTPException) as e:
            self._apply("SEAL-3")
        self.assertEqual(e.exception.status_code, 403)
        self.assertIn("clearance del suo owner", e.exception.detail)

    def test_an_admin_is_bound_by_the_dm_owners_clearance_too(self):
        with self.assertRaises(HTTPException):
            self._apply("SEAL-3", who="admin")

    def test_the_preview_applies_the_same_cap(self):
        with self.assertRaises(HTTPException):
            self._run(lambda: T.topic_tier_preview("SEAL-0", "dm-clodia--davide",
                                                   _Req(), to="SEAL-3"))

    def test_the_agent_below_the_new_tier_is_reported(self):
        out = self._run(lambda: T.topic_tier_preview("SEAL-0", "dm-clodia--davide",
                                                     _Req(), to="SEAL-2"))
        self.assertEqual([r["name"] for r in out["lose_access"]], ["clodia"])

    def test_a_topic_is_not_capped_by_this_rule(self):
        topic = {"tier": "SEAL-0", "owner": "davide", "participants": ["davide"]}
        out = self._run(lambda: T.topic_tier_preview("SEAL-0", "acme", _Req(), to="SEAL-4"),
                        meta=topic, owner_clearance="SEAL-0")
        self.assertEqual(out["to"], "SEAL-4")


if __name__ == "__main__":
    unittest.main()
