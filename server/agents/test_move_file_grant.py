"""`topic.move_file`: a chi va, e cosa deve insegnare la skill.

clodia-platform#419. Il verbo nasce perché il suo surrogato — `topic.fetch` +
`topic.put` + `topic.delete_file` — non è equivalente: il `put` scrive una
provenienza nuova, quindi riordinare una cartella di allegati email cancella il
flag `untrusted` dei documenti di terzi. Qui si difendono le due metà che stanno
in questo repo: **chi** ha il verbo, e **cosa** gli viene insegnato.

(27 ott 2026, clodia-platform#626: i seed non dichiarano più verbi, quindi la
metà del «chi» — sotto — ora vive nelle tuple del gateway e non si verifica qui.)

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

from ..config import workspace_path

PACK = Path(workspace_path("catalogs/packs/base-pack"))
AGENTS = PACK / "agents"
SKILL = PACK / "plugins" / "base-pack" / "skills" / "topic-files" / "SKILL.md"
COSTITUZIONE = PACK / "constitutions" / "platform-core.md"

VERBO = "topic.move_file"


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
