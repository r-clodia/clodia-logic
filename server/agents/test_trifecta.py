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
        """Nessuno dei due ha tutti i lati da solo: insieme sì.

        Misura la CAPACITÀ, non il punteggio: da quando `score` conta i bit del
        vettore (contaminato · dati privati · uscita ARBITRARIA), un canale pulito
        non arriva a 3 nemmeno con la composizione completa — ed è il punto della
        modifica. Ciò che questo test difende è la chiusura: i lati si sommano
        fra partecipanti.
        """
        prof = self._profile(["lettore", "postino"])
        self.assertEqual(prof["capability"], 3)
        self.assertTrue(prof["capability_legs"]["egress"])
        self.assertTrue(prof["capability_legs"]["private_data"])
        self.assertEqual(prof["by_leg"]["egress"], ["postino"])
        self.assertEqual(prof["by_leg"]["private_data"], ["lettore"])

    def test_symbols_follow_the_score(self) -> None:
        """Il punteggio conta i BIT ACCESI. Senza contaminazione il primo è 0, e
        un lettore che non fa uscire niente è 1/3 (solo dati privati)."""
        self.assertEqual(self._profile(["lettore"])["label"], "1/3")
        self.assertEqual(self._profile(["lettore"])["symbol"], "✅")
        self.assertEqual(self._profile(["muto"])["label"], "0/3")
        # e con la contaminazione lo stesso canale sale
        prof = trifecta.context_profile(["lettore"], specs=self._specs(),
                                        tainted=True)
        self.assertEqual(prof["label"], "2/3")
        self.assertEqual(prof["vector"], "110")

    def test_human_participant_does_not_raise_the_score(self) -> None:
        self.assertEqual(self._profile(["muto", "davide"])["score"], 0)

    def test_closure_includes_agents_reachable_by_invitation(self) -> None:
        # "recluta" non ha alcun lato, ma può portare chiunque nel canale.
        prof = self._profile(["recluta"])
        # capacità: la chiusura porta dentro i lati di chi è invitabile
        self.assertEqual(prof["capability"], 3)
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
        self.assertEqual(prof["capability"], 3)

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
        self.assertEqual(p["capability"], 3)
        self.assertEqual(p["score"], 2)  # niente contaminazione → primo bit spento


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
                            {"mode": mode, "egress": {"scope": "listed"}})
                self.assertEqual(p["egress_scope"], "arbitrary")
                self.assertEqual(p["residual"], p["score"])

    def test_gate_mode_makes_egress_presided_and_lowers_the_residual(self):
        p = self._p(["topic.open", "web.fetch", "email.send"],
                    {"mode": "gate", "egress": {"scope": "none"}})
        self.assertEqual(p["score"], 3)          # the capability is still there
        self.assertEqual(p["egress_scope"], "presided")
        self.assertEqual(p["residual"], 2)       # a human stands in the way

    def test_a_star_rule_is_arbitrary_even_when_enforced(self):
        """`["*"]` is declared but constrains nothing."""
        p = self._p(["topic.open", "web.fetch", "email.send"],
                    {"mode": "on", "egress": {"scope": "wide"}})
        self.assertEqual(p["egress_scope"], "arbitrary")
        self.assertEqual(p["residual"], 3)

    def test_all_types_muted_counts_as_no_egress_at_all(self):
        p = self._p(["topic.open", "email.send"],
                    {"mode": "on", "egress": {"scope": "muted"}})
        self.assertEqual(p["egress_scope"], "none")
        self.assertEqual(p["residual"], 1)

    def test_an_agent_without_egress_verbs_has_scope_none(self):
        p = self._p(["topic.open"], {"mode": "gate", "egress": {"scope": "none"}})
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
        self.assertEqual(p["capability"], 3)
        # `tainted` non passato → primo bit `?`: non inventato né a 0 né a 1.
        self.assertEqual(p["vector"], "?11")
        self.assertIsNone(p["tainted"])
        self.assertEqual(p["residual"], p["score"])  # alias: il punteggio È il residuo
        self.assertEqual(max(a["residual"] for a in p["agents"]), 2)

    def test_a_channel_under_gate_reports_the_mode_and_a_lower_residual(self):
        specs = [_spec("a", ["topic.open", "web.fetch", "email.send"])]
        with patch.object(trifecta, "egress_confinement",
                          return_value={"mode": "gate", "egress": {"scope": "none"}}):
            p = trifecta.context_profile(["a"], specs=specs, config=self.CFG)
        self.assertEqual(p["capability"], 3)
        self.assertEqual(p["vector"], "?10")  # uscita presidiata, taint ignoto
        self.assertEqual(p["residual"], p["score"])  # alias: il punteggio È il residuo
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


class RemoteEgressTests(unittest.TestCase):
    """Un remote non vagliato accende il terzo bit (richiesta del 4 ago 2026).

    Un remote non è un verbo: è un condotto PERMANENTE. Un topic collegato a una
    cartella Drive fa uscire i propri file da lì per definizione, e se quella
    cartella non è fra le destinazioni approvate l'uscita è arbitraria —
    indipendentemente da quali verbi abbiano i partecipanti.
    """

    CFG = {
        "version": 1,
        "private_data": {"include": ["topic.open"], "exclude": []},
        "untrusted_input": {"include": ["web.*"], "exclude": []},
        "egress": {"include": ["email.send"], "exclude": []},
        "expansion": {"include": ["agents.*"], "exclude": []},
    }

    def _p(self, remote_egress):
        specs = [_spec("lettore", ["topic.open"])]   # nessun verbo di uscita
        with patch.object(trifecta, "egress_confinement",
                          return_value={"mode": "gate", "egress": {"scope": "none"}}):
            return trifecta.context_profile(["lettore"], specs=specs, config=self.CFG,
                                            tainted=False, remote_egress=remote_egress)

    def test_an_unvetted_remote_lights_the_third_bit_with_no_egress_verbs(self):
        p = self._p(True)
        self.assertEqual(p["vector"], "011")
        self.assertTrue(p["remote_egress"])
        # e la capacità NON cambia: nessun partecipante ha verbi di uscita
        self.assertFalse(p["capability_legs"]["egress"])

    def test_a_vetted_remote_leaves_it_off(self):
        p = self._p(False)
        self.assertEqual(p["vector"], "010")
        self.assertFalse(p["remote_egress"])

    def test_the_reason_is_distinguishable_from_an_agents_arbitrary_egress(self):
        """Un remote non vagliato è un problema di whitelist, un agente con uscita
        arbitraria è un problema di grant: si risolvono con azioni diverse."""
        self.assertTrue(self._p(True)["remote_egress"])
        self.assertFalse(self._p(False)["remote_egress"])


class RemoteUriTests(unittest.TestCase):
    def test_a_drive_remote_becomes_a_folder_uri(self):
        self.assertEqual(
            trifecta.remote_uri({"remote": {"type": "drive",
                                            "config": {"folder": "1AbC"}}}),
            "gdrive:folder/1AbC")

    def test_a_git_remote_is_its_url(self):
        self.assertEqual(
            trifecta.remote_uri({"remote": {"type": "git",
                                            "config": {"url": "https://github.com/a/b"}}}),
            "https://github.com/a/b")

    def test_no_remote_no_uri(self):
        for meta in ({}, {"remote": {}}, {"remote": {"type": "drive", "config": {}}}):
            self.assertIsNone(trifecta.remote_uri(meta))

    def test_membership_is_unknown_without_the_orchestrator_secret(self):
        """Non si inventa né sì né no: il chiamante tratta `None` come non
        vagliato, che è la direzione prudente."""
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(trifecta.uri_allowed("gdrive:folder/1AbC"))


class EgressScopeShapeTests(unittest.TestCase):
    """`egress_scope` legge la forma GLOBALE della whitelist (#128).

    Leggeva ancora `conf["agents"][name]`, che dopo il passaggio alla lista globale
    è sempre assente. Con modo `gate` usciva "presided" per caso giusto, ma **un
    `*` non veniva più rilevato**: una lista che apre tutto risultava presidiata.
    È la direzione d'errore che questa misura non può permettersi, ed è il tipo di
    difetto che un cambio di payload lascia dietro senza rompere niente.
    """

    def _scope(self, conf):
        return trifecta.egress_scope("x", conf, egress_lit=True)

    def test_a_star_is_arbitrary_under_both_enforcing_modes(self):
        for mode in ("gate", "on"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    self._scope({"mode": mode, "egress": {"scope": "wide"}}),
                    "arbitrary")

    def test_gate_with_no_declared_destination_is_presided(self):
        self.assertEqual(self._scope({"mode": "gate", "egress": {"scope": "none"}}),
                         "presided")

    def test_on_with_no_declared_destination_is_no_egress(self):
        """In `on` una lista vuota non lascia passare niente: non è uscita."""
        self.assertEqual(self._scope({"mode": "on", "egress": {"scope": "none"}}),
                         "none")

    def test_on_with_a_list_is_listed(self):
        self.assertEqual(self._scope({"mode": "on", "egress": {"scope": "listed"}}),
                         "listed")

    def test_an_unreadable_shape_is_arbitrary(self):
        """Gateway muto: non si inventa un confinamento."""
        for conf in ({"mode": "gate"}, {"mode": "on", "egress": {}}):
            with self.subTest(conf=conf):
                self.assertEqual(self._scope(conf), "arbitrary")

    def test_no_egress_verbs_short_circuits(self):
        self.assertEqual(
            trifecta.egress_scope("x", {"mode": "gate", "egress": {"scope": "wide"}},
                                  egress_lit=False),
            "none")
