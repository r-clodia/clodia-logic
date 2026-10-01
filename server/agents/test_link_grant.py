"""`topic.link_add`: a chi va, e cosa deve insegnare la skill.

clodia-platform#477. Collegare due topic fa vedere a OGNI partecipante di
ciascuno i file dell'altro, in sola lettura. È la stessa domanda di
`topic.add_participant` — chi entra nel perimetro di una stanza — con una
risposta più larga: non un agente, una stanza intera.

Qui si difendono le due metà che stanno in questo repo: **chi** ha il verbo, e
**cosa** gli viene insegnato.

Sul «chi» l'invariante non è un elenco di nomi ma una regola: il collegamento va
solo dove c'è già `topic.add_participant`. Un seed che non può far entrare un
agente in una stanza non deve poterci far entrare un'altra stanza intera.
Scritta come regola invece che come lista, regge anche al prossimo seed.

Sul «cosa»: la skill `topic-files` è il testo che l'agente legge quando lavora
sui file di un topic, ed è lì che deve stare la metà non ovvia — il mount è in
SOLA LETTURA, e il modo di aggirarla (copiarsi il file qui) è proprio ciò che il
collegamento serve a non fare. Stessa famiglia di `test_move_file_grant`: una
capacità nuova che tocca lo `agent.yaml` e dimentica il testo accanto lascia il
seed a posto sulla carta e l'agente che sbaglia lo stesso.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from ..config import workspace_path
from .inheritance import effective_tool_permissions

PACK = Path(workspace_path("catalogs/packs/base-pack"))
AGENTS = PACK / "agents"
SKILL = PACK / "plugins" / "base-pack" / "skills" / "topic-files" / "SKILL.md"

VERBO = "topic.link_add"


def _seeds() -> dict:
    out = {}
    for d in sorted(AGENTS.iterdir()):
        f = d / "agent.yaml"
        if f.is_file():
            out[d.name] = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return out


class GrantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.seeds = _seeds()
        self.grants = {n: set(effective_tool_permissions(n, self.seeds))
                       for n in self.seeds}

    def test_link_goes_only_where_add_participant_already_is(self) -> None:
        for nome, g in self.grants.items():
            if VERBO in g:
                with self.subTest(seed=nome):
                    self.assertIn(
                        "topic.add_participant", g,
                        f"{nome} ha {VERBO} senza topic.add_participant: potrebbe "
                        "far entrare nel perimetro di una stanza un'altra stanza "
                        "intera pur non potendoci far entrare un singolo agente")

    def test_whoever_can_link_can_unlink(self) -> None:
        """Un seed che propone collegamenti e non può ritirarli produce
        perimetri che crescono soltanto, e lascia all'owner una sola direzione."""
        for nome, g in self.grants.items():
            if VERBO in g:
                with self.subTest(seed=nome):
                    self.assertIn("topic.link_remove", g)

    def test_at_least_one_seed_actually_has_it(self) -> None:
        """Le due regole sopra sono vere anche se nessuno ha il verbo: senza
        questo controllo, una capacità dichiarata e concessa a nessuno
        passerebbe per un invariante rispettato."""
        self.assertTrue([n for n, g in self.grants.items() if VERBO in g],
                        "nessun seed ha topic.link_add: il verbo esiste nel "
                        "gateway e non lo può chiedere nessuno")


class SkillTests(unittest.TestCase):
    def test_the_skill_says_the_linked_mount_is_read_only(self) -> None:
        testo = SKILL.read_text(encoding="utf-8").lower()
        self.assertIn("sola lettura", testo)
        self.assertIn("collegato", testo)

    def test_the_skill_says_the_label_travels_with_the_file(self) -> None:
        """La parte che un agente sbaglierebbe da solo: «viene da un canale
        amico» non ripulisce un allegato di terzi."""
        testo = SKILL.read_text(encoding="utf-8").lower()
        self.assertIn("untrusted", testo)


if __name__ == "__main__":
    unittest.main()
