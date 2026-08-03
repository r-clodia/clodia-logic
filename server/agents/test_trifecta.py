"""Test del danger score trifecta (issue clodia-platform#77, step 1: calcolo)."""
from __future__ import annotations

import pathlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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

    def test_instance_override_is_additive_not_replacing(self) -> None:
        """Un override d'istanza PARZIALE non deve azzerare i lati che non
        dichiara: sostituendo, un agente realmente 3/3 finirebbe mostrato 0/3 —
        falsa rassicurazione, la sola direzione d'errore inaccettabile qui."""
        import tempfile
        from unittest.mock import patch
        tmp = pathlib.Path(tempfile.mkdtemp()) / "trifecta.yaml"
        tmp.write_text("version: 99\negress:\n  - custom.push\n", encoding="utf-8")
        spec = _spec("x", tools=["topic.read_file", "web.fetch", "email.send"])
        with patch.object(trifecta, "data_path", lambda rel: tmp):
            cfg = trifecta.load_config(force=True)
            self.assertEqual(cfg["version"], 99)          # la version è quella dell'override
            for leg in trifecta.LEGS:                     # nessun lato azzerato
                self.assertTrue(cfg[leg]["include"], f"lato '{leg}' azzerato dall'override")
            self.assertIn("custom.push", cfg["egress"]["include"])   # pattern aggiunto
            self.assertEqual(trifecta.agent_profile(spec, cfg)["score"], 3)
        trifecta.load_config(force=True)                  # ripristina la cache dal repo

    def test_merge_does_not_duplicate_patterns(self) -> None:
        base = trifecta._parse_config({"egress": ["email.send"]})
        extra = trifecta._parse_config({"egress": ["email.send", "telegram.send"]})
        merged = trifecta._merge_config(base, extra)
        self.assertEqual(merged["egress"]["include"], ["email.send", "telegram.send"])

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


class FailClosedTests(unittest.TestCase):
    """#119 — un namespace che il catalogo non conosce non è «innocuo».

    Il difetto misurato in produzione: `tool_permissions` con soli
    `slack.post_message` / `dropbox.upload` dava **0/3**. Non era il difetto di
    §9 (voci note mancanti, aggiunte a mano): era la REGOLA DI DEFAULT.
    """

    CFG = {
        "version": 1,
        "private_data": {"include": ["email.*"], "exclude": []},
        "untrusted_input": {"include": ["web.*"], "exclude": []},
        "egress": {"include": ["email.send"], "exclude": []},
        "expansion": {"include": ["agents.*"], "exclude": []},
    }

    def _p(self, tools):
        return trifecta.agent_profile(_spec("x", tools), config=self.CFG)

    def test_an_unknown_namespace_is_assumed_able_to_read_and_to_send(self):
        p = self._p(["slack.post_message", "dropbox.upload"])
        self.assertEqual(p["score"], 2)
        self.assertTrue(p["legs"]["private_data"])
        self.assertTrue(p["legs"]["egress"])
        # non untrusted_input: marcarlo renderebbe ogni pack nuovo 3/3
        # all'istante, che è rumore e non informazione
        self.assertFalse(p["legs"]["untrusted_input"])
        self.assertEqual(p["unclassified"], ["dropbox", "slack"])

    def test_the_reason_says_it_is_unclassified_not_which_verb(self):
        """«acceso da email.send» e «acceso perché slack è ignoto» richiedono
        azioni diverse: senza la distinzione l'operatore non sa quale."""
        why = self._p(["slack.post_message"])["why"]
        self.assertIn("slack.* (namespace non classificato)", why["egress"])

    def test_a_known_namespace_deliberately_in_no_leg_stays_at_zero(self):
        """La regola riguarda l'IGNOTO, non l'escluso.

        `gdrive.rename` è il precedente documentato: scrittura di solo
        metadato, deliberatamente in nessun lato. Un namespace citato negli
        `exclude` è noto e non va toccato.
        """
        cfg = dict(self.CFG)
        cfg["egress"] = {"include": ["gdrive.*"], "exclude": ["gdrive.rename"]}
        p = trifecta.agent_profile(_spec("x", ["gdrive.rename"]), config=cfg)
        self.assertEqual(p["score"], 0)
        self.assertEqual(p["unclassified"], [])

    def test_the_wildcard_is_not_an_unknown(self):
        """`*` combacia con ogni pattern classificato: accende tutto da sé, e
        non va contato come namespace ignoto (era l'argomento per cui esplodere
        il `*` di clodia sarebbe stato un peggioramento — #104 §8)."""
        p = self._p(["*"])
        self.assertEqual(p["unclassified"], [])
        self.assertEqual(p["score"], 3)

    def test_a_classified_namespace_keeps_its_own_legs(self):
        p = self._p(["web.fetch"])
        self.assertEqual(p["unclassified"], [])
        self.assertTrue(p["legs"]["untrusted_input"])
        self.assertFalse(p["legs"]["egress"])

    def test_a_channel_reports_which_namespaces_are_unclassified(self):
        """Un canale a 3/3 «perché nessuno ha classificato slack» è un problema
        di catalogo, non di composizione: si risolve con una riga di yaml, non
        togliendo un partecipante. La UI deve poterli distinguere."""
        specs = [_spec("a", ["slack.post_message"]), _spec("b", ["web.fetch"])]
        p = trifecta.context_profile(["a", "b"], specs=specs, config=self.CFG)
        self.assertEqual(p["unclassified"], ["slack"])
        self.assertEqual(p["score"], 3)


class ConfinementScoreTests(unittest.TestCase):
    """#104 §7 property 4 — circumscribed egress is not arbitrary egress.

    `score` stays the CAPABILITY (it does not lie: the agent does hold those
    verbs); `residual` is what is left once the applied confinement is taken into
    account. Before this, both got 3/3 and the number could not discriminate.
    """

    CFG = {
        "version": 1,
        "private_data": {"include": ["topic.*"], "exclude": []},
        "untrusted_input": {"include": ["web.*"], "exclude": []},
        "egress": {"include": ["email.send"], "exclude": []},
        "expansion": {"include": ["agents.*"], "exclude": []},
    }

    def _p(self, tools, conf):
        return trifecta.agent_profile(_spec("x", tools), config=self.CFG,
                                     egress_conf=conf)

    def test_a_mode_that_does_not_enforce_leaves_egress_arbitrary(self):
        """A confinement that is not applied is not a confinement. Counting it
        would lower the score of an agent that can still send freely — a lie in
        the worst direction."""
        for mode in ("report", "off", "unknown"):
            with self.subTest(mode=mode):
                p = self._p(["topic.open", "web.fetch", "email.send"],
                            {"mode": mode, "agents": {"x": {"email": {"scope": "listed", "count": 2}}}})
                self.assertEqual(p["egress_scope"], "arbitrary")
                self.assertEqual(p["residual"], p["score"])

    def test_gate_mode_makes_egress_presided_and_lowers_the_residual(self):
        p = self._p(["topic.open", "web.fetch", "email.send"],
                    {"mode": "gate", "agents": {}})
        self.assertEqual(p["score"], 3)          # the capability is still there
        self.assertEqual(p["egress_scope"], "presided")
        self.assertEqual(p["residual"], 2)       # a human stands in the way

    def test_a_star_rule_is_arbitrary_even_when_enforced(self):
        """`["*"]` is declared but constrains nothing."""
        p = self._p(["topic.open", "web.fetch", "email.send"],
                    {"mode": "on", "agents": {"x": {"email": {"scope": "wide", "count": 1}}}})
        self.assertEqual(p["egress_scope"], "arbitrary")
        self.assertEqual(p["residual"], 3)

    def test_all_types_muted_counts_as_no_egress_at_all(self):
        p = self._p(["topic.open", "email.send"],
                    {"mode": "on", "agents": {"x": {"email": {"scope": "muted", "count": 0}}}})
        self.assertEqual(p["egress_scope"], "none")
        self.assertEqual(p["residual"], 1)

    def test_an_agent_without_egress_verbs_has_scope_none(self):
        p = self._p(["topic.open"], {"mode": "gate", "agents": {}})
        self.assertEqual(p["egress_scope"], "none")

    def test_a_channel_residual_is_the_or_of_the_legs_not_the_max(self):
        """An agent at 2/3 without egress plus one at 1/3 with arbitrary egress
        makes a channel with residual 3, while the max of the per-agent residuals
        would say 2. The closure is the unit of evaluation here too."""
        specs = [_spec("reader", ["topic.open", "web.fetch"]),
                 _spec("sender", ["email.send"])]
        conf = {"mode": "report", "agents": {}}   # not enforcing → arbitrary
        with patch.object(trifecta, "egress_confinement", return_value=conf):
            p = trifecta.context_profile(["reader", "sender"], specs=specs,
                                        config=self.CFG)
        self.assertEqual(p["score"], 3)
        self.assertEqual(p["residual"], 3)
        self.assertEqual(max(a["residual"] for a in p["agents"]), 2)

    def test_a_channel_under_gate_reports_the_mode_and_a_lower_residual(self):
        specs = [_spec("a", ["topic.open", "web.fetch", "email.send"])]
        with patch.object(trifecta, "egress_confinement",
                          return_value={"mode": "gate", "agents": {}}):
            p = trifecta.context_profile(["a"], specs=specs, config=self.CFG)
        self.assertEqual(p["score"], 3)
        self.assertEqual(p["residual"], 2)
        self.assertEqual(p["egress_mode"], "gate")
        self.assertEqual(p["egress_scopes"], ["presided"])

    def test_an_unreachable_gateway_does_not_invent_a_confinement(self):
        """Best-effort read: if the gateway does not answer, the score stays the
        pure capability. A confinement imagined is worse than one not seen."""
        with patch.dict("os.environ", {}, clear=True):
            trifecta._EGRESS_CACHE = None
            self.assertEqual(trifecta.egress_confinement(force=True)["mode"],
                             "unknown")
            trifecta._EGRESS_CACHE = None


class GrantNegationTests(unittest.TestCase):
    """#104 §8 — un grant `-verbo` sottrae da un namespace concesso in blocco.

    Senza questo lo scorer vedeva `topic.*` combaciare con `topic.remote_push`
    nel catalogo e continuava ad accendere l'uscita: enforcement applicato dal
    PDP, misura cieca. È la divergenza peggiore possibile — un numero che
    descrive un sistema diverso da quello che gira.
    """

    CFG = {
        "version": 1,
        "private_data": {"include": ["topic.open", "topic.files"], "exclude": []},
        "untrusted_input": {"include": ["topic.files"], "exclude": []},
        "egress": {"include": ["topic.remote_push", "topic.remote_commit"],
                   "exclude": []},
        "expansion": {"include": ["agents.*"], "exclude": []},
    }

    def _p(self, tools):
        return trifecta.agent_profile(_spec("x", tools), config=self.CFG,
                                     egress_conf={"mode": "off", "agents": {}})

    def test_negating_every_egress_verb_of_a_namespace_turns_the_leg_off(self):
        p = self._p(["topic.*", "-topic.remote_push", "-topic.remote_commit"])
        self.assertFalse(p["legs"]["egress"])
        self.assertTrue(p["legs"]["private_data"])   # il resto del namespace resta

    def test_negating_only_some_leaves_the_leg_on(self):
        """Una riduzione parziale non va arrotondata a sicura."""
        p = self._p(["topic.*", "-topic.remote_push"])
        self.assertTrue(p["legs"]["egress"])

    def test_negating_a_namespace_verb_does_not_disable_a_wildcard_pattern(self):
        """Se il catalogo classifica `web.*` e il grant nega `-web.fetch`, il
        namespace resta acceso: un intero namespace non è coperto dalla negazione
        di un suo verbo, e trattarlo così sarebbe una falsa rassicurazione."""
        cfg = dict(self.CFG)
        cfg["untrusted_input"] = {"include": ["web.*"], "exclude": []}
        p = trifecta.agent_profile(_spec("x", ["web.*", "-web.fetch"]), config=cfg,
                                   egress_conf={"mode": "off", "agents": {}})
        self.assertTrue(p["legs"]["untrusted_input"])

    def test_a_negated_punctual_grant_lights_nothing(self):
        p = self._p(["-topic.open"])
        self.assertEqual(p["score"], 0)

    def test_a_negation_is_not_reported_as_a_reason(self):
        """Nel `why` devono comparire i grant che ACCENDONO, non quelli tolti."""
        p = self._p(["topic.open", "-topic.files"])
        self.assertNotIn("-topic.files", p["why"]["private_data"])


class GateDiscountTests(unittest.TestCase):
    """#126 — un verbo che passa da un umano non pesa come uno autonomo.

    Il residuo scontava solo la whitelist di DESTINAZIONE (`egress_scope`); il
    M-gate, che chiede conferma umana a OGNI chiamata, non contava affatto: un
    grant `settings.*` pesava come se l'agente potesse usarlo da solo. Lo sconto
    vale per tutti e tre i lati e sta in `residual`; `score` resta la capacità.
    """

    #: Forma di ciò che il gateway restituisce su `/internal/gate/spec`. In
    #: produzione arriva da lì e NON è duplicato nel codice sotto test: la copia
    #: sta qui, in un fixture, proprio perché divergere sia un problema del test
    #: e non della misura.
    GATED = {
        "prefixes": ["settings.", "pki.", "ca."],
        "exact": ["web.post", "topic.add_participant", "topic.remove_participant",
                  "mcp.add", "mcp.remove", "packs.import_url", "packs.remove",
                  "packs.install_pip", "packs.install_npm",
                  "providers.pause", "providers.resume",
                  "agents.grant_tool", "agents.revoke_tool"],
    }

    CFG = {
        "version": 1,
        "private_data": {"include": ["settings.*", "agents.*", "topic.read_file"],
                         "exclude": []},
        "untrusted_input": {"include": ["web.*", "topic.read_file"], "exclude": []},
        "egress": {"include": ["web.post", "email.send"], "exclude": []},
        "expansion": {"include": ["agents.*"], "exclude": []},
    }

    #: Nessun confinamento di destinazione: così l'unico sconto osservabile è
    #: quello del gate, e i due non si coprono a vicenda nei test.
    OPEN = {"mode": "off", "agents": {}}

    def _p(self, tools, gated=None, conf=None):
        return trifecta.agent_profile(_spec("x", tools), config=self.CFG,
                                     egress_conf=conf or self.OPEN,
                                     gated=self.GATED if gated is None else gated)

    # ── la regola su un singolo grant ────────────────────────────────────
    def test_a_punctual_gated_verb_is_presided(self):
        self.assertTrue(trifecta.grant_is_gated("web.post", self.GATED))
        self.assertTrue(trifecta.grant_is_gated("settings.backup_run", self.GATED))

    def test_a_namespace_is_presided_only_if_the_whole_family_is_gated(self):
        """`settings.` è un prefisso gated: ogni verbo della famiglia passa da un
        umano, quindi `settings.*` è presidiato. `agents.*` no: il gate copre
        `grant_tool`/`revoke_tool`, non `agents.list`."""
        self.assertTrue(trifecta.grant_is_gated("settings.*", self.GATED))
        self.assertTrue(trifecta.grant_is_gated("settings", self.GATED))   # ns nudo
        self.assertFalse(trifecta.grant_is_gated("agents.*", self.GATED))

    def test_a_wildcard_over_a_partly_gated_namespace_is_not_presided(self):
        """`topic.*` include `topic.add_participant` (gated) e `topic.read_file`
        (no): presidiato per un decimo. Contarlo come presidiato sarebbe la falsa
        rassicurazione che questa misura non può permettersi."""
        self.assertFalse(trifecta.grant_is_gated("topic.*", self.GATED))

    def test_the_full_wildcard_is_never_presided(self):
        self.assertFalse(trifecta.grant_is_gated("*", self.GATED))
        self.assertEqual(self._p(["*"])["residual"], 3)

    def test_a_negation_is_not_something_to_preside(self):
        self.assertFalse(trifecta.grant_is_gated("-settings.set", self.GATED))

    # ── lo sconto per lato ───────────────────────────────────────────────
    def test_a_leg_lit_only_by_gated_grants_leaves_the_residual(self):
        p = self._p(["settings.*"])
        self.assertTrue(p["legs"]["private_data"])   # la capacità c'è
        self.assertEqual(p["score"], 1)              # e `score` non mente
        self.assertTrue(p["gated_legs"]["private_data"])
        self.assertEqual(p["residual"], 0)           # ma nessuna chiamata è autonoma

    def test_one_ungated_grant_is_enough_to_keep_the_leg(self):
        """Il grant non presidiato è la strada che resta aperta: un lato
        presidiato al 90% non è un lato presidiato."""
        p = self._p(["settings.*", "topic.read_file"])
        self.assertFalse(p["gated_legs"]["private_data"])
        self.assertEqual(p["ungated"]["private_data"], ["topic.read_file"])
        self.assertTrue(p["residual_legs"]["private_data"])

    def test_ungated_names_the_grants_to_remove(self):
        """È la parte azionabile: dice QUALI grant impediscono lo sconto."""
        p = self._p(["web.post", "email.send"])
        self.assertEqual(p["ungated"]["egress"], ["email.send"])
        self.assertEqual(p["residual"], 1)

    def test_the_gate_discounts_egress_even_with_no_destination_whitelist(self):
        """Le due mitigazioni sono alternative: presidiare l'uscita per VERBO
        vale quanto presidiarla per destinazione."""
        p = self._p(["web.post"])
        self.assertEqual(p["egress_scope"], "arbitrary")   # nessuna whitelist
        self.assertTrue(p["gated_legs"]["egress"])
        self.assertEqual(p["residual"], 0)

    def test_a_destination_whitelist_still_discounts_an_ungated_verb(self):
        """Nessuna regressione sul confinamento di #104 §7: `email.send` non è
        gated, ma sotto `gate` la destinazione passa da un umano."""
        p = self._p(["email.send"], conf={"mode": "gate", "agents": {}})
        self.assertFalse(p["gated_legs"]["egress"])
        self.assertEqual(p["residual"], 0)

    def test_all_three_legs_can_be_discounted(self):
        """La §7 scontava solo l'uscita: un agente i cui unici grant sono tutti
        gated risultava 3/3 residuo. Ora resta 3/3 di CAPACITÀ e 0 di residuo."""
        cfg = dict(self.CFG)
        cfg["untrusted_input"] = {"include": ["settings.*"], "exclude": []}
        cfg["egress"] = {"include": ["settings.backup_run"], "exclude": []}
        p = trifecta.agent_profile(_spec("x", ["settings.*"]), config=cfg,
                                   egress_conf=self.OPEN, gated=self.GATED)
        self.assertEqual(p["score"], 3)
        self.assertEqual(p["residual"], 0)
        self.assertEqual(p["gated_legs"], {leg: True for leg in trifecta.LEGS})

    def test_an_unclassified_namespace_follows_the_same_rule(self):
        """Il fail-closed di #119 accende `private_data`+`egress` su un namespace
        ignoto. La regola dello sconto è UNA per tutti i grant: se quel verbo è
        gated il gate vale, se non lo è il lato resta."""
        gated = self._p(["pki.sign"])
        self.assertEqual(gated["unclassified"], ["pki"])
        self.assertEqual(gated["score"], 2)
        self.assertEqual(gated["residual"], 0)
        loose = self._p(["slack.post_message"])
        self.assertEqual(loose["score"], 2)
        self.assertEqual(loose["residual"], 2)
        self.assertEqual(loose["ungated"]["egress"], ["slack.post_message"])

    # ── il gate non visto non è un gate ──────────────────────────────────
    def test_an_unreadable_gate_set_discounts_nothing(self):
        """Stessa direzione d'errore del confinamento: un gate immaginato
        abbasserebbe il residuo di un agente che agisce da solo. Vale anche per
        un gateway troppo vecchio per conoscere la rotta (404 → insieme vuoto)."""
        p = self._p(["settings.*", "web.post"], gated=trifecta._NO_GATE)
        self.assertEqual(p["residual"], p["score"])
        self.assertFalse(any(p["gated_legs"].values()))

    def test_an_unreachable_gateway_returns_an_empty_gate_set(self):
        with patch.dict("os.environ", {}, clear=True):
            trifecta._GATE_CACHE = None
            self.assertEqual(trifecta.gated_verbs(force=True),
                             {"prefixes": [], "exact": []})
            trifecta._GATE_CACHE = None

    # ── livello canale ───────────────────────────────────────────────────
    def _ctx(self, specs, names):
        with patch.object(trifecta, "egress_confinement", return_value=self.OPEN), \
             patch.object(trifecta, "gated_verbs", return_value=self.GATED):
            return trifecta.context_profile(names, specs=specs, config=self.CFG)

    def test_a_channel_is_presided_only_if_every_agent_is(self):
        """La mitigazione è per-agente: presidiare l'uscita di uno non presidia
        quella di un altro, e l'OR va fatto sui lati RESIDUI dei singoli."""
        specs = [_spec("presidiato", ["web.post"]), _spec("libero", ["email.send"])]
        p = self._ctx(specs, ["presidiato", "libero"])
        self.assertEqual(p["score"], 2)             # untrusted_input + egress
        self.assertEqual(p["residual"], 1)          # `libero` esce da solo
        self.assertFalse(p["gated_legs"]["egress"])
        self.assertEqual(p["residual_legs"],
                         {"private_data": False, "untrusted_input": False,
                          "egress": True})
        # …togliendo l'agente non presidiato lo sconto compare.
        q = self._ctx(specs, ["presidiato"])
        self.assertEqual(q["score"], 2)
        self.assertEqual(q["residual"], 0)
        self.assertTrue(q["gated_legs"]["egress"])

    def test_a_channel_reports_whether_the_gate_set_was_readable(self):
        """Un canale senza sconti perché il gateway non risponde non è un canale
        senza gate: le due letture richiedono azioni opposte."""
        specs = [_spec("a", ["web.post"])]
        self.assertTrue(self._ctx(specs, ["a"])["gate_visible"])
        with patch.object(trifecta, "egress_confinement", return_value=self.OPEN), \
             patch.object(trifecta, "gated_verbs", return_value=trifecta._NO_GATE):
            p = trifecta.context_profile(["a"], specs=specs, config=self.CFG)
        self.assertFalse(p["gate_visible"])
        self.assertEqual(p["residual"], p["score"])   # nessuno sconto immaginato


class GateDiscountOnRealSeedsTests(unittest.TestCase):
    """Effetto MISURATO dello sconto sui seed reali, con il catalogo reale.

    Serve come regressione sull'unica direzione d'errore inaccettabile: uno
    sconto che compare dove non deve. Il caso osservato in #126 è `sysadmin`, e
    va detto chiaramente che la regola «tutti i grant del lato gated» NON lo
    sconta: `settings.*` e `web.post` sono presidiati, ma gli stessi lati sono
    accesi anche da `agents.*` e da `github.issue_write`, che nessun umano vede
    passare. Il numero resta 3/3 perché il rischio resta.
    """

    GATED = GateDiscountTests.GATED
    OPEN = {"mode": "off", "agents": {}}

    def _p(self, name):
        return trifecta.agent_profile(_seed(name), config=trifecta.load_config(),
                                      egress_conf=self.OPEN, gated=self.GATED)

    def test_sysadmin_keeps_its_score_and_its_residual(self):
        p = self._p("sysadmin")
        self.assertEqual(p["score"], 3)
        self.assertEqual(p["residual"], 3, "sconto comparso dove il rischio resta")
        self.assertFalse(any(p["gated_legs"].values()))
        # …e il report dice perché, verbo per verbo.
        self.assertTrue(p["ungated"]["private_data"])
        self.assertTrue(p["ungated"]["egress"])

    def test_no_seed_of_the_base_pack_earns_a_discount_today(self):
        """Misura, non aspettativa: con i grant attuali NESSUNO dei cinque seed
        guadagna lo sconto, perché ogni lato acceso da un verbo gated è acceso
        anche da uno che non lo è (`clodia`/`ophelia` da `*`, `messaggero` da
        `email.*`, `segretario` da `topic.read_file`, `sysadmin` da `agents.*` e
        `github.issue_write`). Il valore di #126 non è abbassare questi numeri —
        è dire quali grant li tengono su. Se un domani un seed viene ridotto e lo
        sconto compare, questo test va aggiornato con la misura nuova, non
        cancellato."""
        for name in ("clodia", "ophelia", "sysadmin", "messaggero", "segretario"):
            p = self._p(name)
            with self.subTest(agent=name):
                self.assertEqual(p["residual"], p["score"])
                self.assertFalse(any(p["gated_legs"].values()))

    def test_every_discounted_leg_has_no_ungated_grant_left(self):
        """La proprietà, sui dati reali: uno sconto senza `ungated` vuoto sarebbe
        un residuo che dichiara presidiato un lato ancora attraversabile."""
        for name in ("clodia", "ophelia", "sysadmin", "messaggero", "segretario"):
            p = self._p(name)
            with self.subTest(agent=name):
                self.assertLessEqual(p["residual"], p["score"])
                for leg in trifecta.LEGS:
                    if p["gated_legs"][leg]:
                        self.assertEqual(p["ungated"][leg], [])
