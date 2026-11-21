"""#580 — the clearance of a caller is the SPAWN's, not its seed's.

`runtime.inspect_topic` recomputed the caller's SEAL from the seed's default
provider. A spawn of `clodia` running on a SEAL-2 provider (its room's) was
told its own SEAL-2 room was above its clearance; the converse — a spawn
moved to a weaker provider while the seed default is stronger — would have
been a false grant. The session already signs the spawn's effective
clearance in its token (`session._effective_clearance(kind, override)`):
the route now uses that claim. The forced responder of a topic turn is
gated on the provider of THIS room, like the auto-picker.
"""
from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from . import caller_auth
from . import channels as ch

TIER = "SEAL-2"
ROOM = "titulon-tech"
META = {"title": "Titulon", "status": "active", "owner": "davide",
        "participants": ["davide", "clodia"], "tier": TIER}


def _request(body: dict, claims: dict | None, *, gateway: bool = False):
    async def _json():
        return body
    req = SimpleNamespace(json=_json, state=SimpleNamespace(), query_params={},
                          headers={"x-orchestrator-secret": "s"} if gateway else {})
    req.state._caller_claims = claims        # what `caller_claims` memoises
    return req


def _token(clearance: str | None, *, agent: str = "clodia", on_behalf: bool = False) -> dict:
    claims = {"agent": agent, "principal": "davide"}
    if clearance:
        claims["clearance"] = clearance
    if on_behalf:
        claims["on_behalf"] = True
    return claims


class SignedAgentClearanceTests(unittest.TestCase):
    def test_the_agents_own_token_gives_its_signed_clearance(self):
        self.assertEqual(caller_auth.signed_agent_clearance(
            _request({}, _token("SEAL-2")), "clodia"), "SEAL-2")

    def test_another_agents_token_gives_nothing(self):
        self.assertIsNone(caller_auth.signed_agent_clearance(
            _request({}, _token("SEAL-3", agent="sysadmin")), "clodia"))

    def test_an_on_behalf_token_gives_nothing(self):
        self.assertIsNone(caller_auth.signed_agent_clearance(
            _request({}, _token("SEAL-3", on_behalf=True)), "clodia"))

    def test_no_token_or_no_claim_gives_nothing(self):
        self.assertIsNone(caller_auth.signed_agent_clearance(_request({}, None), "clodia"))
        self.assertIsNone(caller_auth.signed_agent_clearance(
            _request({}, _token(None)), "clodia"))


class InspectTopicUsesTheSpawnsClearance(unittest.IsolatedAsyncioTestCase):
    """The real `_can_access`; only the seed's recomputation is pinned."""

    async def _call(self, claims, *, seed_clearance: str, gateway: bool = False,
                    tier: str = TIER):
        spec = SimpleNamespace(name="clodia", type="bot")
        body = {"tier": tier, "name": ROOM, "by": "clodia"}
        env = {"CLODIA_ORCHESTRATOR_SECRET": "s"} if gateway else {}
        with patch.dict(os.environ, env), \
                patch.object(ch.registry, "get_by_name",
                             side_effect=lambda n: spec if n == "clodia" else None), \
                patch.object(ch, "_effective_clearance", return_value=seed_clearance), \
                patch.object(ch.topics_client, "async_open_topic",
                             return_value={"meta": META}), \
                patch.object(ch.topics_client, "async_list_messages", return_value=[]), \
                patch.object(ch.topics_client, "async_list_files", return_value=[]):
            return await ch.runtime_inspect_topic(_request(body, claims, gateway=gateway))

    async def test_a_seal2_spawn_of_a_seal1_seed_inspects_its_seal2_room(self):
        # The terra case of 20 Oct 2026: seed default claude-team (SEAL-1),
        # spawn on aws-region-eu (SEAL-2), its own SEAL-2 room.
        out = await self._call(_token("SEAL-2"), seed_clearance="SEAL-1")
        self.assertTrue(out["ok"])
        self.assertEqual(out["name"], ROOM)

    async def test_a_seal1_spawn_of_a_seal2_seed_cannot(self):
        with self.assertRaises(HTTPException) as cm:
            await self._call(_token("SEAL-1"), seed_clearance="SEAL-2")
        self.assertEqual(cm.exception.status_code, 403)

    async def test_through_the_gateway_with_the_token_forwarded(self):
        # clodia-tools sends both the paired secret and the caller's token.
        out = await self._call(_token("SEAL-2"), seed_clearance="SEAL-1", gateway=True)
        self.assertTrue(out["ok"])
        with self.assertRaises(HTTPException):
            await self._call(_token("SEAL-1"), seed_clearance="SEAL-2", gateway=True)

    async def test_the_gateway_without_a_token_falls_back_to_the_seed(self):
        out = await self._call(None, seed_clearance="SEAL-2", gateway=True)
        self.assertTrue(out["ok"])
        with self.assertRaises(HTTPException):
            await self._call(None, seed_clearance="SEAL-1", gateway=True)

    async def test_the_signed_clearance_is_no_ceiling_breaker(self):
        # A signed SEAL-2 does not open a SEAL-3 room.
        with self.assertRaises(HTTPException) as cm:
            await self._call(_token("SEAL-2"), seed_clearance="SEAL-3", tier="SEAL-3")
        self.assertEqual(cm.exception.status_code, 403)


class ForcedResponderUsesTheRoomsProvider(unittest.TestCase):
    """`responder_hint` is gated on the provider the agent runs on IN THIS
    ROOM (`_provider_seal_ok`, the auto-picker's gate and the provider the
    session starts on), not on the seed's default provider."""

    class _Started(Exception):
        """Raised by `manager.reserve`: the turn got past the responder gate."""

    def _run(self, *, room_seal_ok: bool):
        spec = SimpleNamespace(name="clodia", type="bot")
        seen: list = []

        def _seal_ok(s, tier):
            seen.append((s.name, tier))
            return room_seal_ok

        def _reserve(chat_id, grace=None):
            raise self._Started(chat_id)

        with patch.object(ch.registry, "get_by_name",
                          side_effect=lambda n: spec if n == "clodia" else None), \
                patch.object(ch, "_effective_clearance",
                             side_effect=AssertionError("seed recomputation used")), \
                patch.object(ch, "_provider_seal_ok", side_effect=_seal_ok), \
                patch.object(ch.manager, "reserve", _reserve):
            try:
                out = asyncio.run(ch.run_topic_turn(
                    TIER, ROOM, dict(META), trigger_text="ciao", responder_hint="clodia"))
            except self._Started as e:
                out = ("started", str(e))
        return seen, out

    def test_a_room_provider_at_the_tier_takes_the_turn(self):
        # Seed default below the tier is irrelevant: `_effective_clearance`
        # raises if consulted.
        seen, out = self._run(room_seal_ok=True)
        self.assertEqual(seen, [("clodia", TIER)])
        self.assertEqual(out, ("started", f"chan:{TIER}:{ROOM}:clodia"))

    def test_a_room_provider_below_the_tier_refuses(self):
        seen, out = self._run(room_seal_ok=False)
        self.assertEqual(seen, [("clodia", TIER)])
        self.assertEqual(out, (None, None))


if __name__ == "__main__":
    unittest.main()
