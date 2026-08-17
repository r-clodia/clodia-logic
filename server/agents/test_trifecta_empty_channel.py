"""Un canale senza dati portati dentro non accende il bit dei dati privati.

Osservazione dell'owner, 17 ago 2026:

    «il trifecta va 3/3 perché parte sempre da 1/3 in quanto si assume che un
    canale abbia sempre un fs da proteggere, non è questo il caso del canale
    software-house che ha soltanto working files, scratch etc. non dovrebbe
    partire da 1/3 ma da 0/3»

Misurato su quel canale: tutti e sette i partecipanti accendevano
`private_data`, e per **tutti** veniva dai verbi che leggono QUESTO canale
(`topic.read_file`, `topic.files`, `topic.fetch`, `topic.read_document`) —
nessuno da fuori. E il canale conteneva solo file `provenance: agent`: patch e
materiale che gli agenti avevano prodotto lavorando.

La regola non è però «fs vuoto → 0», e questo file fissa la differenza: chi legge
ANCHE la memoria, la posta o altri topic tiene il bit acceso su un canale vuoto,
perché quel canale non lo rende innocuo. Una rassicurazione falsa è peggio di un
allarme prudente.
"""
from __future__ import annotations

import unittest

from . import trifecta
from .models import AgentSpec


def _ag(nome: str, verbi: list[str]) -> AgentSpec:
    return AgentSpec.model_validate({
        "name": nome, "description": "d", "display_name": nome, "model": "m",
        "system_prompt": "s.md", "tool_permissions": verbi, "native_tools": [],
    })


#: legge solo questo canale — il caso di fullstack-dev e security-engineer
DENTRO = ["topic.files", "topic.read_file", "topic.read_document", "topic.fetch"]
#: legge anche fuori: memoria dell'agente
FUORI = ["memory.read"]


class PrivateDataScopeTests(unittest.TestCase):
    def test_channel_only_verbs_are_scoped_to_the_channel(self) -> None:
        cfg = trifecta.load_config()
        p = trifecta.agent_profile(_ag("dev", DENTRO), cfg)
        self.assertTrue(p["legs"]["private_data"])
        self.assertEqual("channel", p["private_data_scope"])

    def test_reading_memory_makes_it_external(self) -> None:
        cfg = trifecta.load_config()
        p = trifecta.agent_profile(_ag("mix", DENTRO + FUORI), cfg)
        self.assertEqual("external", p["private_data_scope"])

    def test_no_private_verbs_at_all(self) -> None:
        cfg = trifecta.load_config()
        p = trifecta.agent_profile(_ag("muto", ["artifact.render"]), cfg)
        self.assertEqual("none", p["private_data_scope"])


class ChannelWithoutDataTests(unittest.TestCase):
    def _profilo(self, agenti, ha_dati):
        return trifecta.context_profile(
            [a.name for a in agenti], specs=agenti,
            tainted=False, remote_egress=False, channel_has_data=ha_dati)

    def test_a_channel_with_only_working_files_does_not_light_the_bit(self) -> None:
        """Il caso `software-house`: scende, ed è ciò che l'owner chiedeva."""
        prof = self._profilo([_ag("dev", DENTRO)], ha_dati=False)
        self.assertEqual(0, prof["bits"]["private_data"])
        self.assertTrue(prof["private_data_suppressed"])

    def test_the_same_channel_with_data_lights_it(self) -> None:
        prof = self._profilo([_ag("dev", DENTRO)], ha_dati=True)
        self.assertEqual(1, prof["bits"]["private_data"])
        self.assertFalse(prof["private_data_suppressed"])

    def test_an_agent_reading_outside_keeps_it_lit_on_an_empty_channel(self) -> None:
        """Il falso zero che la regola secca «fs vuoto → 0» avrebbe prodotto: un
        canale vuoto non rende innocuo chi legge la posta dell'owner."""
        prof = self._profilo([_ag("mix", DENTRO + FUORI)], ha_dati=False)
        self.assertEqual(1, prof["bits"]["private_data"])
        self.assertFalse(prof["private_data_suppressed"])

    def test_one_external_reader_among_many_is_enough(self) -> None:
        prof = self._profilo([_ag("dev", DENTRO), _ag("mix", FUORI)], ha_dati=False)
        self.assertEqual(1, prof["bits"]["private_data"])

    def test_unknown_content_keeps_the_previous_behaviour(self) -> None:
        """`None` = non si è potuto stabilire: si tiene l'allarme, perché una
        rassicurazione inventata è peggio."""
        prof = self._profilo([_ag("dev", DENTRO)], ha_dati=None)
        self.assertEqual(1, prof["bits"]["private_data"])
        self.assertFalse(prof["private_data_suppressed"])

    def test_the_payload_says_why_the_bit_is_off(self) -> None:
        """Un numero che scende senza dire perché è indistinguibile da un difetto
        di calcolo — ed è così che questa misura ha perso credibilità una volta."""
        prof = self._profilo([_ag("dev", DENTRO)], ha_dati=False)
        self.assertIs(False, prof["channel_has_data"])
        self.assertTrue(prof["private_data_suppressed"])
        self.assertTrue(prof["capability_legs"]["private_data"],
                        "la CAPACITÀ resta dichiarata: i verbi ci sono davvero")


if __name__ == "__main__":
    unittest.main()
