"""Trifecta calcolata sulla STANZA, non sui soli verbi del seed (#604 · R2 #606).

Dall'epic #604 l'autorità sui verbi non è più solo del seed: il gateway decide
ogni chiamata su `seed ∪ stanza` (`main._agent_tool_reachable` = seed risolto ∪
grant scoped ∪ verbi-dato #590 ∪ toolbox della stanza #605). Un punteggio
calcolato sui soli verbi del seed misura un sistema diverso da quello che gira,
e lo fa nella direzione peggiore: un canale in cui l'owner ha abilitato
`email.*` per tutti i partecipanti risultava senza uscita.

Qui si fissano le due cose che l'owner ha deciso il 23 ott 2026:

1. si misura l'insieme RAGGIUNTO (seed ∪ stanza), e `why` dice da dove viene
   ogni accensione — il seed e la toolbox si correggono in posti diversi;
2. l'ESPANSIONE resta sui verbi del seed: il pavimento `topic.*` contiene
   `topic.add_participant`, e contarlo renderebbe espansore ogni partecipante
   di ogni stanza.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from . import trifecta, trifecta_reset
from .models import AgentSpec

#: Nessun confinamento letto dal gateway: i test non fanno rete.
CONF: dict = {}


def _spec(name, tools=()):
    return SimpleNamespace(
        name=name, type="bot", tool_permissions=list(tools),
        sandbox=SimpleNamespace(allow_shell_cmds=[], deny_shell_patterns=[]),
    )


def _ag(nome: str, verbi: list[str]) -> AgentSpec:
    return AgentSpec.model_validate({
        "name": nome, "description": "d", "display_name": nome, "model": "m",
        "system_prompt": "s.md", "tool_permissions": verbi, "native_tools": [],
    })


def _prof(spec, meta):
    return trifecta.agent_profile(spec, trifecta.load_config(), CONF,
                                  all_specs=[spec],
                                  room=trifecta.room_grants(meta, spec.name))


class RoomGrantsTests(unittest.TestCase):
    """Cosa la stanza porta a un agente, nella forma dei grant."""

    def test_every_participant_gets_the_topic_floor(self) -> None:
        """Decisione 2 dell'epic: `topic.*` in ogni stanza, per tutti."""
        self.assertIn(trifecta.ROOM_FLOOR, trifecta.room_grants({}, "dev"))
        self.assertEqual("topic.*", trifecta.ROOM_FLOOR)

    def test_the_toolbox_is_added_and_an_exception_removes_its_entry(self) -> None:
        meta = {"tools": {"enabled": ["email.*", "gdrive.upload"],
                          "except": {"dev": ["gdrive.upload"]}}}
        dev = trifecta.room_grants(meta, "dev")
        altro = trifecta.room_grants(meta, "altro")
        self.assertIn("email.*", dev)
        self.assertNotIn("gdrive.upload", dev)
        self.assertNotIn("-gdrive.upload", dev)
        self.assertIn("gdrive.upload", altro)

    def test_a_point_exception_inside_a_wildcard_becomes_a_negation(self) -> None:
        """Il gateway espande `email.*` e toglie `email.send`: da qui i verbi
        del gateway non si leggono, quindi la sottrazione resta una negazione."""
        meta = {"tools": {"enabled": ["email.*"], "except": {"dev": ["email.send"]}}}
        dev = trifecta.room_grants(meta, "dev")
        self.assertIn("email.*", dev)
        self.assertIn("-email.send", dev)

    def test_imported_data_brings_the_verbs_the_gateway_gives(self) -> None:
        """Gli stessi verbi di `topic_data._READ_VERBS/_WRITE_VERBS`: le due
        liste vivono in processi diversi e una divergenza qui SOTTOSTIMA."""
        meta = {"data": [
            {"kind": "rag", "name": "corpus", "grants": {"a": "read"}},
            {"kind": "datastore", "name": "p/clienti", "grants": {"a": "write",
                                                                  "b": "read"}}]}
        a = trifecta.room_grants(meta, "a")
        b = trifecta.room_grants(meta, "b")
        for verbo in ("rag.search", "rag.list", "rag.collections",
                      "datastore.read", "datastore.write"):
            self.assertIn(verbo, a, verbo)
        self.assertIn("datastore.read", b)
        self.assertNotIn("datastore.write", b)   # grant di sola lettura
        self.assertNotIn("rag.search", b)        # nessun grant su quell'indice

    def test_an_unreadable_toolbox_does_not_become_permissive(self) -> None:
        for rotta in ({"tools": "email.*"}, {"tools": {"enabled": "email.*"}},
                      {"tools": {"enabled": [""], "except": ["dev"]}}, {}):
            self.assertEqual([trifecta.ROOM_FLOOR],
                             trifecta.room_grants(rotta, "dev"), rotta)

    def test_no_room_is_not_an_empty_room(self) -> None:
        """Un contesto senza stanza (un DM, un chiamante che non la passa) non
        prende nemmeno il pavimento: `topic.*` è di un topic."""
        self.assertEqual([], trifecta.room_grants(None, "dev"))


class ReachedSetTests(unittest.TestCase):
    """I due lati statici si calcolano su `seed ∪ stanza`."""

    def test_a_verb_enabled_only_by_the_room_lights_the_leg(self) -> None:
        """Il caso dell'issue: l'owner abilita l'uscita nella stanza, e il seed
        non la dichiara. Prima il canale risultava senza uscita."""
        spec = _spec("fact-checker", ["topic.read_file"])
        meta = {"tools": {"enabled": ["gdrive.upload"]}}
        p = _prof(spec, meta)
        self.assertTrue(p["legs"]["egress"])
        self.assertIn("gdrive.upload (stanza)", p["why"]["egress"])

    def test_why_separates_the_seed_from_the_room(self) -> None:
        """Toccare il seed e toccare la toolbox sono due azioni diverse: senza
        la distinzione il `why` non dice quale delle due serve."""
        spec = _spec("messaggero", ["email.send"])
        p = _prof(spec, {"tools": {"enabled": ["gdrive.upload"]}})
        self.assertIn("email.send", p["why"]["egress"])
        self.assertIn("gdrive.upload (stanza)", p["why"]["egress"])

    def test_a_per_agent_exception_takes_the_verb_off_that_agent(self) -> None:
        """«Tutti tranne X per questo verbo» deve vedersi nel punteggio."""
        meta = {"tools": {"enabled": ["gdrive.upload"],
                          "except": {"ophelia": ["gdrive.upload"]}}}
        libero = _prof(_spec("dev", ["topic.read_file"]), meta)
        esclusa = _prof(_spec("ophelia", ["topic.read_file"]), meta)
        self.assertIn("gdrive.upload (stanza)", libero["why"]["egress"])
        self.assertNotIn("gdrive.upload (stanza)", esclusa["why"]["egress"])

    def test_imported_data_lights_private_data_for_whoever_is_granted(self) -> None:
        """#590: un indice concesso nella stanza porta `rag.search` anche a un
        seed che non ha mai dichiarato `rag.*`."""
        meta = {"data": [{"kind": "rag", "name": "corpus",
                          "grants": {"ricercatore": "read"}}]}
        p = _prof(_spec("ricercatore", ["topic.post_message"]), meta)
        self.assertTrue(p["legs"]["private_data"])
        self.assertIn("rag.search (stanza)", p["why"]["private_data"])

    def test_a_namespace_the_room_enables_falls_closed_if_unclassified(self) -> None:
        """Il fail-closed di #119 vale anche per ciò che arriva dalla stanza:
        un connettore nuovo abilitato qui non deve risultare innocuo."""
        p = _prof(_spec("dev", ["topic.post_message"]),
                  {"tools": {"enabled": ["slack.post_message"]}})
        self.assertIn("slack", p["unclassified"])
        self.assertIn("slack.* (namespace non classificato)",
                      p["why"]["private_data"])

    def test_the_room_never_takes_away_what_the_seed_grants(self) -> None:
        """R1 è ADDITIVA (`room_tools.exception`): l'eccezione sottrae dal dono
        della stanza, non dal seed. Mostrare spenta una gamba che all'agente
        resta accesa è l'errore che questa misura non può permettersi."""
        meta = {"tools": {"enabled": ["gdrive.upload"],
                          "except": {"messaggero": ["email.send"]}}}
        p = _prof(_spec("messaggero", ["email.send"]), meta)
        self.assertTrue(p["legs"]["egress"])
        self.assertIn("email.send", p["why"]["egress"])


class AlignedWithTheGatewayTests(unittest.TestCase):
    """Review of #567 (B1/B1b) after the gateway fix of #429: the score bites
    and filters exactly as `room_tools` does."""

    def test_a_namespace_exception_removes_the_namespace(self) -> None:
        meta = {"tools": {"enabled": ["email.*"],
                          "except": {"x": ["email.*", "topic.*"]}}}
        self.assertEqual(trifecta.room_grants(meta, "x"), [])
        self.assertEqual(trifecta.room_grants(meta, "y"), ["topic.*", "email.*"])

    def test_star_removes_everything_the_room_gives_but_not_the_data(self) -> None:
        meta = {"tools": {"enabled": ["email.*"], "except": {"x": ["*"]}},
                "data": [{"kind": "rag", "name": "c", "grants": {"x": "read"}}]}
        self.assertEqual(trifecta.room_grants(meta, "x"),
                         ["rag.search", "rag.list", "rag.collections"])

    def test_an_exception_never_negates_a_data_verb(self) -> None:
        """B1b: the gateway unions the data verbs outside `_minus`."""
        meta = {"tools": {"except": {"x": ["rag.search"]}},
                "data": [{"kind": "rag", "name": "c", "grants": {"x": "read"}}]}
        g = trifecta.room_grants(meta, "x")
        self.assertIn("rag.search", g)
        self.assertNotIn("-rag.search", g)
        p = _prof(_spec("x", ["topic.post_message"]), meta)
        self.assertIn("rag.search (stanza)", p["why"]["private_data"])

    def test_what_the_gateway_filters_out_gives_nothing_here_either(self) -> None:
        meta = {"tools": {"enabled": ["*", "packs.*", "agents.grant_tool",
                                      "rag.search", "web.f*", "email.send"]}}
        self.assertEqual(trifecta.room_grants(meta, "x"), ["topic.*", "email.send"])

    def test_an_archived_room_keeps_only_the_floor(self) -> None:
        meta = {"status": "archived", "tools": {"enabled": ["email.*"]}}
        self.assertEqual(trifecta.room_grants(meta, "x"), ["topic.*"])

    def test_the_review_scenario_no_longer_reads_zero(self) -> None:
        """Toolbox `email.*`, exceptions `email.*`+`topic.*` for x: now the
        gateway takes both away, and the score agrees — while y keeps them."""
        meta = {"tools": {"enabled": ["email.*"],
                          "except": {"x": ["email.*", "topic.*"]}}}
        y = _prof(_spec("y", ["web.search"]), meta)
        self.assertIn("email.* (stanza)", y["why"]["egress"])
        x = _prof(_spec("x", ["web.search"]), meta)
        self.assertFalse([w for w in x["why"]["egress"] if "(stanza)" in w])

    def test_the_not_enablable_list_matches_the_gateway(self) -> None:
        """Copy of `room_tools._NOT_ENABLABLE`: named one by one."""
        self.assertEqual(trifecta._ROOM_NOT_ENABLABLE, frozenset({
            "agents", "egress", "ingress", "integrations", "jobs", "logs", "mcp",
            "native", "packs", "profile", "providers", "runtime", "settings",
            "copybrain", "fs", "memory", "topic", "datastore", "eu_corpus", "rag"}))

    def test_an_empty_exception_list_does_not_change_the_signature(self) -> None:
        self.assertEqual(trifecta_reset.toolbox_signature({"tools": {"except": {"x": []}}}), "")
        a = trifecta_reset.toolbox_signature({"tools": {"enabled": ["web.fetch"]}})
        b = trifecta_reset.toolbox_signature({"tools": {"enabled": [" web.fetch "]}})
        self.assertEqual(a, b)


class ExpansionStaysOnTheSeedTests(unittest.TestCase):
    """Decisione 2 dell'owner: l'espansione NON si calcola sulla stanza."""

    def test_the_topic_floor_does_not_make_everyone_an_expander(self) -> None:
        """`topic.*` contiene `topic.add_participant`: contarlo renderebbe
        espansore ogni partecipante di ogni stanza, la chiusura sarebbe sempre
        l'intera registry e il punteggio direbbe la stessa cosa ovunque."""
        p = _prof(_spec("dev", ["topic.read_file"]), {"tools": {"enabled": []}})
        self.assertIn("topic.*", trifecta.room_grants({}, "dev"))
        self.assertFalse(p["expands"])

    def test_who_really_invites_is_still_an_expander(self) -> None:
        p = _prof(_spec("clodia", ["topic.add_participant"]), {})
        self.assertTrue(p["expands"])

    def test_the_closure_of_a_room_is_not_the_whole_registry(self) -> None:
        agenti = [_ag("dev", ["topic.read_file"]), _ag("ophelia", ["gdrive.*"])]
        prof = trifecta.context_profile(
            ["dev"], specs=agenti, tainted=False, remote_egress=False,
            channel_private_data=False, room_meta={"tools": {"enabled": []}})
        self.assertEqual([], prof["expanded_by"])
        self.assertEqual([], prof["reachable"])


class ChannelProfileTests(unittest.TestCase):
    """Il profilo del canale, che è ciò che l'owner vede."""

    def _prof(self, agenti, meta):
        return trifecta.context_profile(
            [a.name for a in agenti], specs=agenti, tainted=False,
            remote_egress=False, channel_private_data=False, room_meta=meta)

    def test_the_room_toolbox_is_the_channel_capability(self) -> None:
        agenti = [_ag("dev", ["topic.read_file"])]
        prof = self._prof(agenti, {"tools": {"enabled": ["gdrive.upload"]}})
        self.assertTrue(prof["capability_legs"]["egress"])
        self.assertIn("dev", prof["by_leg"]["egress"])

    def test_without_room_meta_nothing_changes(self) -> None:
        """I chiamanti che non passano la stanza (DM, test, codice esistente)
        misurano il solo seed, come prima."""
        agenti = [_ag("dev", ["topic.post_message"])]
        self.assertFalse(self._prof(agenti, None)["capability_legs"]["egress"])


class ResetBaselineTests(unittest.TestCase):
    """La baseline dell'owner è la composizione PIÙ la toolbox (#606).

    Il reset azzera il terzo bit finché la stanza è quella approvata. Da #605
    l'uscita può arrivare dalla toolbox senza che la composizione cambi: senza
    firmare anche quella, si approva una stanza, si abilita `email.*` per tutti
    e ci si tiene lo zero.
    """

    def test_the_toolbox_is_part_of_what_the_owner_signs(self) -> None:
        vuota = trifecta_reset.composition_epoch(["dev"], {})
        con_email = trifecta_reset.composition_epoch(
            ["dev"], {"tools": {"enabled": ["email.send"]}})
        self.assertNotEqual(vuota, con_email)

    def test_an_empty_toolbox_keeps_the_epoch_of_before(self) -> None:
        """Le baseline già firmate non devono decadere tutte al deploy: decade
        ciò che una toolbox ce l'ha davvero."""
        self.assertEqual(trifecta_reset.composition_epoch(["dev"]),
                         trifecta_reset.composition_epoch(["dev"], {"tools": {}}))
        self.assertEqual("", trifecta_reset.toolbox_signature({"tools": {}}))


if __name__ == "__main__":
    unittest.main()
