"""I seed del base-pack dichiarano il mestiere, non il pavimento.

Osservazione di Davide, 8 ago 2026: «lo yaml del segretario ripete verbi che gli
devono derivare da archseed, ma archseed non risulta essere un suo parent».

Due difetti in una frase, e sono diversi.

**I verbi ripetuti.** Quattro seed su cinque elencavano gli otto verbi base
dell'arciseed. Non è ridondanza innocua: se un seed ripete ciò che fanno tutti,
la domanda «cosa fa questo agente» resta senza risposta, sepolta sotto il
pavimento. `segretario` dichiarava tre verbi di cui due base — il suo mestiere
era **un** verbo, e non si vedeva.

**L'antenato non dichiarato.** Nel gateway `archseed` è antenato implicito di
tutti, e lo resta: un seed non deve poterne uscire omettendolo. Ma implicito non
è una ragione per essere invisibile — chi legge `segretario` non aveva modo di
sapere da dove venissero quei verbi. Ora la relazione è scritta, e il file dice
la verità che il gateway impone comunque.

Il test che conta è l'ultimo: **nessun seed può ridichiarare un verbo del
pavimento**. La ridondanza torna da sola alla prima modifica a mano, e nessuno se
ne accorgerebbe, perché non rompe niente — peggiora solo la leggibilità, che è
esattamente il tipo di difetto che nessun test coglie mai.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from ..config import workspace_path

AGENTS = Path(workspace_path("catalogs/packs/base-pack/agents"))


def _seeds() -> dict:
    out = {}
    for d in sorted(AGENTS.iterdir()):
        f = d / "agent.yaml"
        if f.is_file():
            out[d.name] = yaml.safe_load(f.read_text()) or {}
    return out


class ArchseedTests(unittest.TestCase):
    def test_the_archseed_is_in_the_pack(self):
        self.assertIn("archseed", _seeds())

    def test_it_is_abstract(self):
        self.assertTrue(_seeds()["archseed"].get("abstract"))

    def test_it_declares_no_engine(self):
        """Un antenato non gira: dichiarare un provider suggerirebbe che
        qualcosa possa eseguirlo."""
        a = _seeds()["archseed"]
        for campo in ("model", "providers", "agent_sdk"):
            with self.subTest(campo=campo):
                self.assertNotIn(campo, a)


class TradeTests(unittest.TestCase):
    def test_every_seed_declares_the_archseed_as_an_ancestor(self):
        for nome, y in _seeds().items():
            if nome == "archseed":
                continue
            with self.subTest(seed=nome):
                self.assertIn("archseed", y.get("parents") or [],
                              f"'{nome}' non dichiara da dove vengono i suoi verbi base")

    def test_no_seed_repeats_a_floor_verb(self):
        """Il test che impedisce alla ridondanza di tornare. Non rompe niente —
        peggiora solo la leggibilità, ed è per questo che senza un test
        tornerebbe."""
        base = set(_seeds()["archseed"]["tool_permissions"])
        for nome, y in _seeds().items():
            if nome == "archseed":
                continue
            ripetuti = sorted(set(y.get("tool_permissions") or []) & base)
            with self.subTest(seed=nome):
                self.assertEqual(
                    ripetuti, [],
                    f"'{nome}' ridichiara verbi del pavimento: arrivano già "
                    f"dall'arciseed, e ripeterli seppellisce il suo mestiere")

    def test_a_seed_still_declares_something_of_its_own(self):
        """Un seed che dopo la pulizia non dichiara niente non è un seed: è
        l'arciseed con un nome diverso."""
        for nome, y in _seeds().items():
            if nome == "archseed":
                continue
            with self.subTest(seed=nome):
                self.assertTrue(y.get("tool_permissions"),
                                f"'{nome}' non dichiara alcun mestiere proprio")


if __name__ == "__main__":
    unittest.main()
