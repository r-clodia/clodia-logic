"""Test dello stack agent → providers (lista) → effettivo (21 giu 2026).

Split dei provider per DPA/costi: anthropic-api / claude-pro-max / openai-api /
codex. L'agent dichiara una LISTA ordinata di provider compatibili; a runtime si
sceglie il PRIMO collegato; se nessuno è collegato l'agent è disattivato.
"""
from __future__ import annotations

import unittest

from ..agents.models import AgentSpec
from . import providers as P
from .agent_registry import _provider_fields


def _spec(**kw) -> AgentSpec:
    base = {"name": "a", "description": "d", "model": "claude-sonnet-4-5",
            "display_name": "A", "system_prompt": "s.md"}
    base.update(kw)
    return AgentSpec.model_validate(base)


class CatalogTests(unittest.TestCase):
    def test_four_providers_split(self) -> None:
        for pid in ("anthropic-api", "claude-pro-max", "openai-api", "codex"):
            self.assertIn(pid, P._CATALOG, pid)
        # vecchi id accorpati non esistono più
        self.assertNotIn("anthropic", P._CATALOG)
        self.assertNotIn("openai", P._CATALOG)

    def test_mechanism_per_provider(self) -> None:
        self.assertEqual(P._CATALOG["anthropic-api"]["mechanism"], "apikey")
        self.assertEqual(P._CATALOG["claude-pro-max"]["mechanism"], "subscription")
        self.assertEqual(P._CATALOG["openai-api"]["mechanism"], "apikey")
        self.assertEqual(P._CATALOG["codex"]["mechanism"], "subscription")

    def test_default_order_api_before_subscription(self) -> None:
        self.assertEqual(P.default_providers_for_sdk("claude"),
                         ["anthropic-api", "claude-team", "aws-region-eu", "claude-pro-max"])
        self.assertEqual(P.default_providers_for_sdk("codex"),
                         ["openai-api", "codex"])
        self.assertEqual(P.default_providers_for_sdk("opencode"), ["scaleway"])
        self.assertEqual(P.default_providers_for_sdk(None),
                         ["anthropic-api", "claude-team", "aws-region-eu", "claude-pro-max"])  # default claude


class CandidateTests(unittest.TestCase):
    def test_explicit_list_wins_and_keeps_order(self) -> None:
        self.assertEqual(
            P.candidate_providers(["claude-pro-max", "anthropic-api"], None, "claude"),
            ["claude-pro-max", "anthropic-api"])

    def test_single_provider_backcompat(self) -> None:
        self.assertEqual(P.candidate_providers(None, "openai-api", "claude"),
                         ["openai-api"])

    def test_legacy_alias_normalized(self) -> None:
        self.assertEqual(P.candidate_providers(None, "anthropic", "claude"),
                         ["anthropic-api"])
        self.assertEqual(P.candidate_providers(["openai"], None, "codex"),
                         ["openai-api"])

    def test_unknown_ids_dropped(self) -> None:
        self.assertEqual(P.candidate_providers(["nope", "anthropic-api"], None, "claude"),
                         ["anthropic-api"])

    def test_sdk_fallback_when_empty(self) -> None:
        self.assertEqual(P.candidate_providers(None, None, "codex"),
                         ["openai-api", "codex"])


class EffectiveTests(unittest.TestCase):
    def test_first_connected_wins(self) -> None:
        eff = P.effective_provider(["anthropic-api", "claude-pro-max"], None, "claude",
                                   {"claude-pro-max"})
        self.assertEqual(eff, "claude-pro-max")

    def test_preference_order_when_both_connected(self) -> None:
        eff = P.effective_provider(["anthropic-api", "claude-pro-max"], None, "claude",
                                   {"anthropic-api", "claude-pro-max"})
        self.assertEqual(eff, "anthropic-api")

    def test_none_connected_returns_none(self) -> None:
        eff = P.effective_provider(["anthropic-api", "claude-pro-max"], None, "claude", set())
        self.assertIsNone(eff)

    def test_override_wins_when_usable(self) -> None:
        # override manuale su un provider in lista e attivo → vince sulla preferenza
        eff = P.effective_provider(["claude-pro-max", "anthropic-api"], None, "claude",
                                   {"claude-pro-max", "anthropic-api"},
                                   override="anthropic-api")
        self.assertEqual(eff, "anthropic-api")

    def test_override_ignored_when_not_active(self) -> None:
        # override su provider non connesso → si ripiega sulla preferenza
        eff = P.effective_provider(["claude-pro-max", "anthropic-api"], None, "claude",
                                   {"anthropic-api"}, override="claude-pro-max")
        self.assertEqual(eff, "anthropic-api")

    def test_override_ignored_when_not_in_list(self) -> None:
        # override su provider non dichiarato dall'agent → ignorato
        eff = P.effective_provider(["claude-pro-max", "anthropic-api"], None, "claude",
                                   {"claude-pro-max", "aws-region-eu"},
                                   override="aws-region-eu")
        self.assertEqual(eff, "claude-pro-max")

    def test_topic_tier_picks_min_cost_eligible_provider(self) -> None:
        eff = P.effective_provider_for_tier(
            ["claude-team", "aws-region-eu"], None, "claude",
            {"claude-team", "aws-region-eu"}, "SEAL-1", "claude-sonnet-4-5")
        self.assertEqual(eff, "claude-team")

    def test_topic_tier_skips_cheaper_but_underclassified_provider(self) -> None:
        eff = P.effective_provider_for_tier(
            ["claude-team", "aws-region-eu"], None, "claude",
            {"claude-team", "aws-region-eu"}, "SEAL-2", "claude-sonnet-4-5")
        self.assertEqual(eff, "aws-region-eu")

    def test_topic_tier_returns_none_when_no_provider_meets_tier(self) -> None:
        eff = P.effective_provider_for_tier(
            ["claude-team"], None, "claude",
            {"claude-team"}, "SEAL-2", "claude-sonnet-4-5")
        self.assertIsNone(eff)


class ProviderFieldsTests(unittest.TestCase):
    def test_effective_and_list_exposed(self) -> None:
        f = _provider_fields(_spec(agent_sdk="claude"), {"claude-pro-max"})
        self.assertEqual(f["provider"], "claude-pro-max")
        self.assertEqual(f["providers"],
                         ["anthropic-api", "claude-team", "aws-region-eu", "claude-pro-max"])
        self.assertTrue(f["provider_connected"])

    def test_disabled_when_no_candidate_connected(self) -> None:
        f = _provider_fields(_spec(agent_sdk="codex", model="gpt-5"), {"anthropic-api"})
        self.assertIsNone(f["provider"])
        self.assertEqual(f["providers"], ["openai-api", "codex"])
        self.assertFalse(f["provider_connected"])

    def test_explicit_providers_list(self) -> None:
        f = _provider_fields(_spec(agent_sdk="claude",
                                   providers=["claude-pro-max", "anthropic-api"]),
                             {"anthropic-api"})
        self.assertEqual(f["provider"], "anthropic-api")
        self.assertEqual(f["providers"], ["claude-pro-max", "anthropic-api"])
        self.assertTrue(f["provider_connected"])

    def test_human_has_no_provider(self) -> None:
        f = _provider_fields(_spec(type="human", model=None, system_prompt=None), set())
        self.assertIsNone(f["provider"])
        self.assertTrue(f["provider_connected"])

    def test_undeterminable_not_marked_disconnected(self) -> None:
        f = _provider_fields(_spec(agent_sdk="opencode"), set())
        self.assertIsNone(f["provider"])
        self.assertEqual(f["providers"], [])
        self.assertTrue(f["provider_connected"])


class BedrockModelTests(unittest.TestCase):
    """Traduzione del modello dichiarato → inference-profile EU su Bedrock."""
    _EU = {
        "extra_env": {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "eu.anthropic.claude-sonnet-4-6",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "eu.anthropic.claude-opus-4-6-v1",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        }
    }

    def setUp(self) -> None:
        self._saved = P._CATALOG.get("aws-region-eu"), P._CATALOG.get("anthropic-api")
        P._CATALOG["aws-region-eu"] = dict(self._EU)
        P._CATALOG["anthropic-api"] = {"extra_env": {}}

    def tearDown(self) -> None:
        aws, api = self._saved
        if aws is None: P._CATALOG.pop("aws-region-eu", None)
        else: P._CATALOG["aws-region-eu"] = aws
        if api is None: P._CATALOG.pop("anthropic-api", None)
        else: P._CATALOG["anthropic-api"] = api

    def test_bedrock_maps_tier(self) -> None:
        self.assertEqual(P.bedrock_model_id("aws-region-eu", "claude-sonnet-4-5"),
                         "eu.anthropic.claude-sonnet-4-6")
        self.assertEqual(P.bedrock_model_id("aws-region-eu", "claude-opus-4-8"),
                         "eu.anthropic.claude-opus-4-6-v1")
        self.assertEqual(P.bedrock_model_id("aws-region-eu", "claude-haiku-4-5"),
                         "eu.anthropic.claude-haiku-4-5-20251001-v1:0")

    def test_non_bedrock_returns_none(self) -> None:
        self.assertIsNone(P.bedrock_model_id("anthropic-api", "claude-sonnet-4-5"))

    def test_unknown_tier_returns_none(self) -> None:
        self.assertIsNone(P.bedrock_model_id("aws-region-eu", "gpt-4o"))


if __name__ == "__main__":
    unittest.main()


class ProviderModelsCrossSdkTests(unittest.TestCase):
    """Regressione (1 ago 2026): l'override per-provider `provider_models` deve
    contare anche nei campi API del registry (card + selettore del profilo),
    non solo nel runtime. Caso reale: fullstack-dev (codex/gpt-5.6-sol) con
    fallback claude-team/claude-opus-5 — senza il fix claude-team veniva
    filtrato perché non serve il modello top-level."""

    def _cross(self) -> AgentSpec:
        return _spec(agent_sdk="codex", model="gpt-5.6-sol",
                     providers=["codex", "claude-team"],
                     provider_models={"claude-team": "claude-opus-5"})

    def test_cross_sdk_fallback_listed(self) -> None:
        f = _provider_fields(self._cross(), {"codex", "claude-team"})
        self.assertEqual(f["providers"], ["codex", "claude-team"])
        self.assertEqual(f["provider"], "codex")  # primario connesso vince

    def test_cross_sdk_fallback_effective_when_primary_down(self) -> None:
        f = _provider_fields(self._cross(), {"claude-team"})
        self.assertEqual(f["provider"], "claude-team")

    def test_without_override_model_filter_still_applies(self) -> None:
        f = _provider_fields(_spec(agent_sdk="codex", model="gpt-5.6-sol",
                                   providers=["codex", "claude-team"]),
                             {"codex", "claude-team"})
        self.assertEqual(f["providers"], ["codex"])


class StacksTests(unittest.TestCase):
    """1 seed → N stack (issue clodia-platform#93): normalizzazione ⇄ legacy."""

    def test_stacks_derive_legacy_fields(self) -> None:
        s = _spec(agent_sdk="codex", model=None,
                  stacks=[{"model": "gpt-5.6-sol", "provider": "codex"},
                          {"model": "claude-opus-5", "provider": "claude-team"}])
        self.assertEqual(s.model, "gpt-5.6-sol")
        self.assertEqual(s.providers, ["codex", "claude-team"])
        self.assertEqual(s.provider_models, {"claude-team": "claude-opus-5"})

    def test_legacy_derives_stacks(self) -> None:
        s = _spec(agent_sdk="codex", model="gpt-5.6-sol",
                  providers=["codex", "claude-team"],
                  provider_models={"claude-team": "claude-opus-5"})
        self.assertEqual([(x.model, x.provider) for x in s.stacks],
                         [("gpt-5.6-sol", "codex"), ("claude-opus-5", "claude-team")])

    def test_legacy_single_model_is_special_case(self) -> None:
        # Il modello vecchio (1 LLM, N provider) = N stack con lo stesso model.
        s = _spec(providers=["anthropic-api", "claude-pro-max"])
        self.assertEqual([(x.model, x.provider) for x in s.stacks],
                         [("claude-sonnet-4-5", "anthropic-api"),
                          ("claude-sonnet-4-5", "claude-pro-max")])

    def test_duplicate_provider_rejected(self) -> None:
        with self.assertRaises(Exception):
            _spec(stacks=[{"model": "claude-opus-5", "provider": "anthropic-api"},
                          {"model": "claude-sonnet-4-5", "provider": "anthropic-api"}])

    def test_stacks_win_over_legacy(self) -> None:
        s = _spec(model="ignorato", providers=["codex"],
                  stacks=[{"model": "claude-opus-5", "provider": "claude-team"}])
        self.assertEqual(s.model, "claude-opus-5")
        self.assertEqual(s.providers, ["claude-team"])

    def test_no_providers_no_stacks(self) -> None:
        # Nessun providers dichiarato → stacks vuoto (derivazione SDK a runtime).
        s = _spec()
        self.assertEqual(s.stacks, [])

    def test_provider_fields_expose_stack_api(self) -> None:
        s = _spec(agent_sdk="codex", model=None,
                  stacks=[{"model": "gpt-5.6-sol", "provider": "codex"},
                          {"model": "claude-opus-5", "provider": "claude-team"}])
        f = _provider_fields(s, {"claude-team"})   # primario giù
        self.assertEqual(f["provider"], "claude-team")
        self.assertEqual(f["effective_model"], "claude-opus-5")
        self.assertEqual(f["stacks"],
                         [{"model": "gpt-5.6-sol", "provider": "codex"},
                          {"model": "claude-opus-5", "provider": "claude-team"}])
        by_id = {o["id"]: o["model"] for o in f["provider_options"]}
        self.assertEqual(by_id, {"codex": "gpt-5.6-sol", "claude-team": "claude-opus-5"})

    def test_effective_model_falls_back_to_top_level(self) -> None:
        f = _provider_fields(_spec(agent_sdk="claude"), {"claude-pro-max"})
        self.assertEqual(f["effective_model"], "claude-sonnet-4-5")
