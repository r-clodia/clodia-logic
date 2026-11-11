"""#626 — the agent-server asks the gateway's tuples instead of reading the
seed's `tool_permissions`: the seeds no longer carry verbs. #656 removed the
field and the seed fallbacks: gateway silent ⇒ no grant (except the relay's
ACK decision, which is not an authorisation and keeps answering yes)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from . import authz_grants as G
from ..api import topics_client


class _Base(unittest.TestCase):
    def setUp(self):
        G.clear()
        self.addCleanup(G.clear)
        self.calls = []

    def answers(self, table):
        def fake(agent, verbs, scope=None):
            self.calls.append((agent, tuple(verbs), scope))
            return {v: table.get((agent, v, scope), False) for v in verbs}
        return patch.object(topics_client, "authz_available", fake)

    def mute(self):
        def boom(*a, **k):
            raise topics_client.TopicsClientError("giù")
        return patch.object(topics_client, "authz_available", boom)


class HasTests(_Base):
    def test_cached_and_stale_on_failure(self):
        with self.answers({("clodia", "channel.deliver", "SEAL-1/x"): True}):
            self.assertTrue(G.has("clodia", "channel.deliver", "SEAL-1/x"))
            self.assertTrue(G.has("clodia", "channel.deliver", "SEAL-1/x"))
        self.assertEqual(len(self.calls), 1)                       # cached
        with patch.object(G, "TTL", 0.0), self.mute():
            self.assertTrue(G.has("clodia", "channel.deliver", "SEAL-1/x"))   # last known

    def test_unknown_is_none_and_the_mute_gateway_is_not_hammered(self):
        n = []

        def boom(*a, **k):
            n.append(1)
            raise topics_client.TopicsClientError("giù")
        with patch.object(topics_client, "authz_available", boom):
            self.assertIsNone(G.has("x", "agents.grant_tool"))
            self.assertIsNone(G.has("y", "agents.grant_tool"))
        self.assertEqual(len(n), 1)                                # backoff

    def test_global_grants_skip_denies_and_other_agents(self):
        rows = {"tuples": [
            {"agent": "clodia", "verb": "email.read", "rule": [{"effect": "allow"}]},
            {"agent": "*", "verb": "topic.*", "rule": [{"effect": "allow"}]},
            {"agent": "clodia", "verb": "web.post", "rule": [{"effect": "deny"}]},
            {"agent": "officer", "verb": "jobs.*", "rule": [{"effect": "allow"}]}]}
        with patch.object(topics_client, "authz_tuples", lambda a=None: rows):
            self.assertEqual(G.global_grants("clodia"), ["email.read", "topic.*"])


class RelayTests(_Base):
    def test_the_coordinator_is_asked_in_its_room(self):
        from ..api import channel_relay as R
        with self.answers({("segretario", "channel.deliver", "SEAL-1/x"): False}):
            self.assertFalse(R._can_deliver("segretario", "SEAL-1/x"))
        self.assertEqual(self.calls[-1][2], "SEAL-1/x")

    def test_a_mute_gateway_does_not_drop_requests(self):
        from ..api import channel_relay as R
        with self.mute():
            self.assertTrue(R._can_deliver("segretario", "SEAL-1/x"))

    def test_the_relay_no_longer_reads_the_seed(self):
        """#656: a stale seed declaration cannot turn a mute gateway into a no."""
        from ..api import channel_relay as R
        with self.mute(), patch("server.agents.loader.registry.get_by_name",
                                lambda n: NS(tool_permissions=["topic.*"])):
            self.assertTrue(R._can_deliver("segretario", "SEAL-1/x"))


class AdminComposeTests(_Base):
    def test_admin_from_the_tuples(self):
        from ..api import agent_registry as AR
        with patch.object(AR.registry, "get_by_name", lambda n: NS(name=n)), \
                self.answers({("sysadmin", "agents.grant_tool", None): True}):
            self.assertTrue(AR._agent_can_admin("sysadmin"))
            self.assertFalse(AR._agent_can_admin("officer"))

    def test_admin_unknown_is_no_even_with_a_legacy_seed(self):
        """#656: gateway silent ⇒ no grant, whatever a stale in-memory object
        might still say — the seed is not read any more."""
        from ..api import agent_registry as AR
        with self.mute(), patch.object(AR.registry, "get_by_name",
                                       lambda n: NS(tool_permissions=["agents.*", "*"])):
            self.assertFalse(AR._agent_can_admin("sysadmin"))

    def test_the_welcome_pill_from_the_tuples(self):
        from ..api import topic_playbooks as P
        with patch.object(P.registry, "get_by_name", lambda n: NS(name=n)), \
                self.answers({("segretario", "topic.suggest_team", None): True}):
            self.assertTrue(P._coordinator_can_compose("segretario"))


class SignalsTests(_Base):
    def test_routing_reads_the_global_tuples(self):
        from ..api import responder_routing as RR
        rows = {"tuples": [{"agent": "esperto", "verb": "eu_corpus.search",
                            "rule": [{"effect": "allow"}]},
                           {"agent": "esperto", "verb": "rag.*", "rule": [{"effect": "allow"}]}]}
        with patch.object(topics_client, "authz_tuples", lambda a=None: rows):
            self.assertIn("eu-normativa",
                          RR._agent_collections(NS(name="esperto")))

    def test_the_trifecta_reads_the_global_tuples(self):
        from . import trifecta as T
        rows = {"tuples": [{"agent": "clodia", "verb": "email.send",
                            "rule": [{"effect": "allow"}]}]}
        spec = NS(name="clodia", type="bot", native_tools=None)
        with patch.object(topics_client, "authz_tuples", lambda a=None: rows):
            p = T.agent_profile(spec, all_specs=[spec])
        self.assertTrue(any("email.send" in w for ws in p["why"].values() for w in ws))

    def test_a_mute_gateway_gives_no_verb_to_any_signal(self):
        """#656: gateway silent ⇒ no grant, at every former seed-fallback site
        — a stale `tool_permissions` on the object is not read."""
        from ..api import responder_routing as RR
        from . import trifecta as T

        def boom(*a, **k):
            raise topics_client.TopicsClientError("giù")
        spec = NS(name="esperto", type="bot", native_tools=None,
                  tool_permissions=["rag.*", "eu_corpus.search", "email.send"])
        with patch.object(topics_client, "authz_tuples", boom), \
                patch.object(topics_client, "authz_available", boom):
            self.assertEqual(RR._agent_collections(spec), [])
            p = T.agent_profile(spec, all_specs=[spec])
        self.assertFalse(any("email.send" in w for ws in p["why"].values() for w in ws))


class DriftTests(unittest.TestCase):
    def test_verbs_are_not_compared_any_more(self):
        from . import gateway_drift as D
        self.assertNotIn(("tool_permissions", "allowed_tools"), D.CAMPI)

    def test_the_repair_sends_no_verbs(self):
        """#656: `register_agent` no longer takes or sends `allowed_tools`."""
        from ..api import gateway_admin
        from . import gateway_drift as D
        calls = []
        spec = NS(name="x", gated_tools=None, gated_in_channel=None,
                  profile_tools=None, carries=None, denied_tools=None)
        with patch.object(gateway_admin, "register_agent",
                          lambda *a, **k: calls.append((a, k))):
            D._ripara(spec)
        self.assertEqual(calls[0][0], ("x",))
        self.assertNotIn("allowed_tools", calls[0][1])


class RegisterPayloadTests(unittest.TestCase):
    def test_the_payload_has_no_allowed_tools(self):
        """#656: the gateway ignores `allowed_tools`; the client stops sending
        it, and omits the second-line lists it was not given."""
        from ..api import gateway_admin
        sent = {}

        class _R:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True}

        def post(url, headers=None, json=None, timeout=None):
            sent.update(json or {})
            return _R()
        with patch.object(gateway_admin, "_headers", lambda: {}), \
                patch.object(gateway_admin.requests, "post", post):
            gateway_admin.register_agent("x", gated_tools=["email.send"])
        self.assertEqual(sent, {"agent": "x", "gated_tools": ["email.send"]})


if __name__ == "__main__":
    unittest.main()
