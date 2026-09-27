"""`topic.move_file`: a chi va, e cosa deve insegnare la skill.

clodia-platform#419. Il verbo nasce perché il suo surrogato — `topic.fetch` +
`topic.put` + `topic.delete_file` — non è equivalente: il `put` scrive una
provenienza nuova, quindi riordinare una cartella di allegati email cancella il
flag `untrusted` dei documenti di terzi. Qui si difendono le due metà che stanno
in questo repo: **chi** ha il verbo, e **cosa** gli viene insegnato.

Sul «chi» l'invariante non è un elenco di nomi ma una regola: il move va solo
dove c'è già `delete_file`. Un seed che può mettere file ma non cestinarli, col
move otterrebbe un delete di fatto — rinomina in `local/nascosto/x` e il file non
è più dove chi lo cercava lo cerca — cioè un verbo che il seed deliberatamente
non ha. Scritta come regola invece che come lista, regge anche al prossimo seed.

Sul «cosa», il difetto che ha prodotto la issue è stato **testuale**: la skill
`topic-files` e il principio 6 della costituzione insegnavano fetch+put come il
modo di «spostare» un file, e un agente che segue le istruzioni che ha ricevuto
faceva esattamente il danno. Stessa famiglia di `test_seed_mandates`: una
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
COSTITUZIONE = PACK / "constitutions" / "platform-core.md"

VERBO = "topic.move_file"


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

    def test_move_goes_only_where_delete_already_is(self) -> None:
        """Il move è anche un modo di togliere un file da dove sta: concederlo a
        chi non può cestinare sarebbe concedere un delete di fatto."""
        for nome, g in self.grants.items():
            if VERBO in g:
                with self.subTest(seed=nome):
                    self.assertIn(
                        "topic.delete_file", g,
                        f"{nome} ha {VERBO} senza topic.delete_file: rinominando "
                        "otterrebbe la sparizione di un file da dove stava, che è "
                        "esattamente ciò che delete_file concede")

    def test_the_seeds_that_already_tidy_files_have_it(self) -> None:
        """Il contrario della regola sopra: dove il delete c'è e il put pure, il
        move non deve mancare — se no resta in piedi il surrogato pericoloso."""
        for nome, g in self.grants.items():
            if {"topic.delete_file", "topic.put"} <= g:
                with self.subTest(seed=nome):
                    self.assertIn(
                        VERBO, g,
                        f"{nome} può mettere e cestinare file ma non spostarli: "
                        "per riordinare userebbe fetch+put+delete, che lava la "
                        "provenienza (clodia-platform#419)")

    def test_at_least_one_seed_actually_has_it(self) -> None:
        """Le due regole sopra sono entrambe vere anche se nessuno ha il verbo:
        senza questa riga il modulo passerebbe a vuoto."""
        self.assertTrue([n for n, g in self.grants.items() if VERBO in g])


class TextTests(unittest.TestCase):
    """Il testo che l'agente legge deve nominare il verbo e smentire il surrogato."""

    def test_the_skill_teaches_the_verb(self) -> None:
        txt = SKILL.read_text(encoding="utf-8")
        self.assertIn(VERBO, txt)

    def test_the_skill_says_that_copy_and_reupload_is_not_a_move(self) -> None:
        """Nominare il verbo non basta: la skill descriveva fetch+put come il
        modo di spostare i file, ed è quella frase che va contraddetta."""
        txt = SKILL.read_text(encoding="utf-8").lower()
        self.assertIn("provenienza", txt)
        self.assertIn("non è un move", txt)

    def test_the_constitution_points_at_the_verb_too(self) -> None:
        """Il principio 6 parla di «spostare documenti da/verso un topic» ed è
        nel contesto di OGNI agente: se resta muto sul move, la skill la legge
        solo chi ce l'ha."""
        self.assertIn(VERBO, COSTITUZIONE.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
