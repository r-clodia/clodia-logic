"""Il secondo bit è un FATTO sul canale, non una capacità dei presenti.

Definizione dell'owner, 17 ago 2026 (decision record 36):

    «il secondo bit setta se al canale sono stati aggiunti dati di natura
     riservata e non generati dagli agenti, ad esempio un file uploaded oppure
     un attachment di email, oppure un collegamento ad un remote»

Prima era l'OR dei verbi di lettura dei partecipanti, e per questo era acceso
quasi sempre: chiunque possa stare in un canale ha i verbi per leggerne i file.
Misurato su `software-house` il 17 ago: tutti e sette i partecipanti lo
accendevano, e il canale conteneva solo patch prodotte dagli agenti. Un bit
acceso su tutto non discrimina, ed era l'unico lavoro che gli si chiedeva.

Con la nuova definizione la capacità NON sparisce: resta in `capability_legs`,
che non mente — quei verbi ci sono davvero. Cambia cosa misura il punteggio: la
stanza, non chi ci sta dentro.
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


#: legge i file del canale — il caso di fullstack-dev
LETTORE = ["topic.files", "topic.read_file", "topic.read_document", "topic.fetch"]
#: legge anche fuori dal canale: la posta dell'owner
LETTORE_ESTERNO = LETTORE + ["memory.read"]


class SecondBitIsAFactTests(unittest.TestCase):
    def _profilo(self, agenti, riservati):
        return trifecta.context_profile(
            [a.name for a in agenti], specs=agenti, tainted=False,
            remote_egress=False, channel_private_data=riservati)

    def test_only_agent_produced_files_leaves_it_off(self) -> None:
        """Il caso `software-house`, che ha prodotto la definizione."""
        prof = self._profilo([_ag("dev", LETTORE)], riservati=False)
        self.assertEqual(0, prof["bits"]["private_data"])

    def test_data_brought_in_lights_it(self) -> None:
        prof = self._profilo([_ag("dev", LETTORE)], riservati=True)
        self.assertEqual(1, prof["bits"]["private_data"])

    def test_it_does_not_depend_on_who_can_read(self) -> None:
        """La differenza con la definizione precedente, in due assert.

        Un canale CON dati riservati e nessun lettore accende il bit: i dati sono
        lì. Un canale SENZA dati e con un lettore onnivoro no: non c'è niente da
        leggere. Prima era l'opposto in entrambi i casi.
        """
        senza_lettori = self._profilo([_ag("muto", ["artifact.render"])], riservati=True)
        self.assertEqual(1, senza_lettori["bits"]["private_data"])

        lettore_a_vuoto = self._profilo([_ag("mix", LETTORE_ESTERNO)], riservati=False)
        self.assertEqual(0, lettore_a_vuoto["bits"]["private_data"])

    def test_the_capability_is_still_declared(self) -> None:
        """Il punteggio scende, la capacità resta esposta: negarla sarebbe la
        sola bugia che questa misura non può permettersi."""
        prof = self._profilo([_ag("dev", LETTORE)], riservati=False)
        self.assertTrue(prof["capability_legs"]["private_data"])
        self.assertTrue(prof["private_data_suppressed"])
        self.assertIs(False, prof["channel_private_data"])

    def test_unknown_falls_back_to_capability(self) -> None:
        """`None` = non stabilito: si tiene l'allarme. Un `False` inventato
        sarebbe una rassicurazione su dati che potrebbero esserci."""
        prof = self._profilo([_ag("dev", LETTORE)], riservati=None)
        self.assertEqual(1, prof["bits"]["private_data"])
        self.assertFalse(prof["private_data_suppressed"])

    def test_a_channel_with_no_readers_and_no_data_is_zero(self) -> None:
        prof = self._profilo([_ag("muto", ["artifact.render"])], riservati=False)
        self.assertEqual(0, prof["bits"]["private_data"])
        self.assertFalse(prof["capability_legs"]["private_data"])


if __name__ == "__main__":
    unittest.main()
