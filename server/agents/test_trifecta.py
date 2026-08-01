"""Test del danger score trifecta (issue clodia-platform#77, step 1: calcolo)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import yaml

from ..config import workspace_path
from . import trifecta
from .models import AgentSpec

SEEDS_DIR = workspace_path("catalogs/packs/base-pack/agents")


def _spec(name, tools=(), kind="normal", shell=(), deny_shell=()):
    return SimpleNamespace(
        name=name, type=kind, tool_permissions=list(tools),
        sandbox=SimpleNamespace(allow_shell_cmds=list(shell),
                                deny_shell_patterns=list(deny_shell)),
    )


def _seed(name) -> AgentSpec:
    raw = yaml.safe_load((SEEDS_DIR / name / "agent.yaml").read_text(encoding="utf-8"))
    return AgentSpec.model_validate(raw)


class ConfigTests(unittest.TestCase):
    """La classificazione è configurazione versionata, non costanti nel codice."""

    def test_repo_config_is_loaded_and_non_empty(self) -> None:
        cfg = trifecta.load_config(force=True)
        self.assertGreaterEqual(cfg["version"], 1)
        for leg in trifecta.LEGS:
            self.assertTrue(cfg[leg]["include"], f"lato '{leg}' senza pattern")
        self.assertTrue(cfg["expansion"]["include"])

    def test_malformed_config_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            trifecta._parse_config({"egress": "email.send"})  # stringa, non lista

    def test_exceptions_are_parsed_apart(self) -> None:
        cfg = trifecta._parse_config({"egress": ["email.*", "-email.read"]})
        self.assertEqual(cfg["egress"], {"include": ["email.*"], "exclude": ["email.read"]})


class OverlapTests(unittest.TestCase):
    """Il match fra grant e verbo classificato è una SOVRAPPOSIZIONE."""

    def test_wildcard_grant_covers_specific_verb(self) -> None:
        self.assertTrue(trifecta._overlap("email.*", "email.send"))
        self.assertTrue(trifecta._overlap("*", "topic.read_file"))
        self.assertTrue(trifecta._overlap("topic", "topic.read_file"))  # namespace nudo

    def test_specific_grant_matches_namespace_classification(self) -> None:
        self.assertTrue(trifecta._overlap("telegram.send_message", "telegram.*"))

    def test_different_namespace_or_verb_never_overlaps(self) -> None:
        self.assertFalse(trifecta._overlap("email.send", "telegram.send"))
        self.assertFalse(trifecta._overlap("topic.write_file", "topic.read_file"))

    def test_exception_removes_only_the_grants_it_fully_covers(self) -> None:
        leg = {"include": ["email.*"], "exclude": ["email.send"]}
        self.assertEqual(trifecta._matching_grants(["email.send"], leg), [])
        self.assertEqual(trifecta._matching_grants(["email.*"], leg), ["email.*"])
        self.assertEqual(trifecta._matching_grants(["email.read"], leg), ["email.read"])


class AgentProfileTests(unittest.TestCase):
    """Profilo per agente dai grant effettivi."""

    def test_full_wildcard_is_three_of_three(self) -> None:
        p = trifecta.agent_profile(_spec("clodia", ["*"], kind="super"))
        self.assertEqual(p["score"], 3)
        self.assertEqual(p["legs"], {leg: True for leg in trifecta.LEGS})

    def test_internal_write_verbs_are_not_egress(self) -> None:
        # Scrivere DENTRO Clodia non è uscita: nessun lato acceso.
        p = trifecta.agent_profile(
            _spec("scriba", ["topic.write_file", "topic.post_message", "memory.put"]))
        self.assertEqual(p["score"], 0)

    def test_reading_a_topic_is_private_and_untrusted_not_egress(self) -> None:
        # È il profilo degli specialisti a 2/3 misurato nell'issue.
        p = trifecta.agent_profile(_spec("segretario", ["topic.read_file"]))
        self.assertEqual(p["legs"]["private_data"], True)
        self.assertEqual(p["legs"]["untrusted_input"], True)
        self.assertEqual(p["legs"]["egress"], False)

    def test_send_only_agent_is_egress_only(self) -> None:
        # Chi ha SOLO `email.send` non legge la posta: 1/3, non 3/3. È il
        # falso positivo che le eccezioni della classificazione evitano.
        p = trifecta.agent_profile(_spec("postino", ["email.send"]))
        self.assertEqual(p["score"], 1)
        self.assertTrue(p["legs"]["egress"])
        # …mentre il grant largo sull'intero namespace accende tutti i lati.
        self.assertEqual(trifecta.agent_profile(_spec("m", ["email.*"]))["score"], 3)

    def test_why_reports_the_grants_that_lit_each_leg(self) -> None:
        p = trifecta.agent_profile(_spec("messaggero", ["email.*", "topic.read_file"]))
        self.assertIn("email.*", p["why"]["egress"])
        self.assertIn("topic.read_file", p["why"]["private_data"])

    def test_human_principal_contributes_nothing(self) -> None:
        p = trifecta.agent_profile(_spec("davide", ["*"], kind="human"))
        self.assertEqual(p["score"], 0)
        self.assertTrue(p["human"])

    def test_shell_flag_is_independent_of_the_score(self) -> None:
        with_shell = trifecta.agent_profile(_spec("s", ["topic.read_file"], shell=["*"]))
        self.assertTrue(with_shell["shell"])
        self.assertEqual(with_shell["score"], 2)  # la shell NON è un quarto lato
        denied = trifecta.agent_profile(_spec("m", [], shell=["git"], deny_shell=["*"]))
        self.assertFalse(denied["shell"])
        self.assertFalse(trifecta.agent_profile(_spec("n", []))["shell"])


class SeedAgentsTests(unittest.TestCase):
    """Il calcolo riproduce la misura riportata nell'issue per i seed del base-pack."""

    def test_seed_scores_match_the_issue_table(self) -> None:
        expected = {"clodia": 3, "ophelia": 3, "sysadmin": 3, "messaggero": 3,
                    "segretario": 2}
        got = {n: trifecta.agent_profile(_seed(n))["score"] for n in expected}
        self.assertEqual(got, expected)

    def test_segretario_is_two_thirds_because_it_cannot_send(self) -> None:
        p = trifecta.agent_profile(_seed("segretario"))
        self.assertFalse(p["legs"]["egress"])
        self.assertEqual(p["why"]["egress"], [])

    def test_seed_shell_flags(self) -> None:
        self.assertTrue(trifecta.agent_profile(_seed("sysadmin"))["shell"])
        self.assertTrue(trifecta.agent_profile(_seed("clodia"))["shell"])
        self.assertFalse(trifecta.agent_profile(_seed("messaggero"))["shell"])


class ContextProfileTests(unittest.TestCase):
    """Il profilo del canale è l'OR dei partecipanti, sulla chiusura."""

    def _specs(self):
        return [
            _spec("lettore", ["topic.read_file"]),                 # privati + non fidato
            _spec("postino", ["email.send"]),                      # uscita
            _spec("muto", ["topic.write_file"]),                   # niente
            _spec("recluta", ["topic.add_participant"]),           # può allargare
            _spec("sysadmin", ["agents.*", "settings.*"], shell=["*"]),
            _spec("davide", ["*"], kind="human"),
        ]

    def _profile(self, participants):
        return trifecta.context_profile(participants, specs=self._specs())

    def test_composition_lights_the_third_leg(self) -> None:
        # Nessuno dei due è 3/3 da solo: insieme sì.
        prof = self._profile(["lettore", "postino"])
        self.assertEqual(prof["score"], 3)
        self.assertEqual(prof["label"], "3/3")
        self.assertEqual(prof["symbol"], "🚨")
        self.assertEqual(prof["by_leg"]["egress"], ["postino"])
        self.assertEqual(prof["by_leg"]["private_data"], ["lettore"])

    def test_symbols_follow_the_score(self) -> None:
        self.assertEqual(self._profile(["lettore"])["symbol"], "⚠️")   # 2/3
        self.assertEqual(self._profile(["lettore"])["label"], "2/3")
        self.assertEqual(self._profile(["postino"])["symbol"], "✅")    # 1/3
        self.assertEqual(self._profile(["muto"])["label"], "0/3")

    def test_human_participant_does_not_raise_the_score(self) -> None:
        self.assertEqual(self._profile(["muto", "davide"])["score"], 0)

    def test_closure_includes_agents_reachable_by_invitation(self) -> None:
        # "recluta" non ha alcun lato, ma può portare chiunque nel canale.
        prof = self._profile(["recluta"])
        self.assertEqual(prof["score"], 3)
        self.assertEqual(prof["expanded_by"], ["recluta"])
        self.assertIn("postino", prof["reachable"])
        # …e il punteggio dei soli presenti resta distinto, per non appiattire
        # tutto su 3/3 senza spiegazione.
        self.assertEqual(prof["direct"]["score"], 0)
        self.assertEqual(prof["direct"]["label"], "0/3")

    def test_closure_excludes_humans_and_current_members(self) -> None:
        prof = self._profile(["recluta", "lettore"])
        self.assertNotIn("davide", prof["reachable"])
        self.assertNotIn("lettore", prof["reachable"])

    def test_without_an_expander_the_closure_is_the_participants(self) -> None:
        prof = self._profile(["lettore", "postino"])
        self.assertEqual(prof["reachable"], [])
        self.assertEqual(prof["expanded_by"], [])
        self.assertEqual(prof["direct"]["score"], prof["score"])

    def test_shell_is_reported_separately(self) -> None:
        prof = self._profile(["lettore", "sysadmin"])
        self.assertTrue(prof["shell"])
        self.assertEqual(prof["shell_agents"], ["sysadmin"])
        self.assertEqual(prof["direct"]["shell_agents"], ["sysadmin"])
        self.assertNotIn("shell", prof["legs"])  # non è un quarto lato

    def test_shell_of_a_merely_reachable_agent_is_not_attributed_to_members(self) -> None:
        # "recluta" non ha shell: la shell è di sysadmin, che è solo invitabile.
        prof = self._profile(["recluta"])
        self.assertTrue(prof["shell"])
        self.assertEqual(prof["shell_agents"], ["sysadmin"])
        self.assertEqual(prof["direct"]["shell_agents"], [])

    def test_unknown_participant_is_reported_not_invented(self) -> None:
        prof = self._profile(["lettore", "fantasma"])
        self.assertEqual(prof["unknown_participants"], ["fantasma"])
        self.assertEqual([a["name"] for a in prof["agents"]], ["lettore"])

    def test_empty_channel_is_zero(self) -> None:
        prof = self._profile([])
        self.assertEqual(prof["score"], 0)
        self.assertEqual(prof["agents"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
