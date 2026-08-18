"""Un seed incoerente deve poter essere VISTO, non solo loggato.

clodia-platform#227, punto residuo. I due controlli di `_incoerenze` esistono da
clodia-logic#303, ma finiscono in `LOG.warning` e si fermano lì: non entrano in
`errors()`, non escono da nessuna API, non compaiono in nessuna pagina. Cioè il
rilevatore meccanico del difetto scrive su un canale che nessuno guarda — che è
la stessa forma del difetto che doveva rilevare («non fallisce, rimuove; l'unico
rilevatore è un umano che lo nota settimane dopo»).

`warnings()` sta accanto a `errors()` e non dentro: sono due esiti diversi dello
stesso load. `errors` = lo spec NON carica, l'agente non esiste. `warnings` =
carica, esiste, e si contraddice. Fonderli farebbe sparire un agente funzionante
dalla lista per un avviso, o declasserebbe un errore a nota.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from .loader import AgentRegistry

BOT_INCOERENTE = """\
name: devseed
display_name: Dev
description: d
model: claude-sonnet-4-5
system_prompt: system-prompt.md
native_tools: ["Grep"]
sandbox:
  allow_shell_cmds: ["git", "pytest"]
"""

BOT_COERENTE = """\
name: lettore
display_name: Lettore
description: d
model: claude-sonnet-4-5
system_prompt: system-prompt.md
native_tools: ["Grep", "Glob"]
sandbox:
  allow_shell_cmds: []
"""

UMANO = """\
name: davide
display_name: Davide
description: d
type: human
"""

ROTTO = """\
name: non-corrisponde-alla-cartella
display_name: Rotto
description: d
model: claude-sonnet-4-5
system_prompt: system-prompt.md
"""


class RegistryWarningsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        for nome, testo in (("devseed", BOT_INCOERENTE), ("lettore", BOT_COERENTE),
                            ("davide", UMANO), ("rotto", ROTTO)):
            d = base / nome
            d.mkdir()
            (d / "agent.yaml").write_text(testo)
        self.reg = AgentRegistry(base_dir=base)
        self.reg.load()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_incoherent_seed_is_in_warnings(self) -> None:
        avvisi = self.reg.warnings()
        self.assertIn("devseed", avvisi)
        self.assertTrue(any("non ha modo di eseguire" in a
                            for a in avvisi["devseed"]), avvisi)

    def test_the_message_survives_the_load(self) -> None:
        """L'avviso deve nominare i comandi anche a valle del load: è ciò che
        rende agibile quello che si legge nella pagina, non solo nel log."""
        self.assertTrue(any("pytest" in a
                            for a in self.reg.warnings()["devseed"]), self.reg.warnings())

    def test_a_coherent_seed_has_no_entry(self) -> None:
        """Nessuna chiave, non una lista vuota: un dizionario di avvisi vuoto è
        il modo in cui la pagina sa di non dover mostrare niente."""
        self.assertNotIn("lettore", self.reg.warnings())

    def test_a_human_has_no_entry(self) -> None:
        self.assertNotIn("davide", self.reg.warnings())

    def test_warnings_do_not_hide_the_agent(self) -> None:
        """Un seed incoerente resta caricato e usabile: si segnala, non si punisce."""
        self.assertIsNotNone(self.reg.get_by_name("devseed"))

    def test_an_error_is_not_a_warning(self) -> None:
        """I due canali restano separati: lo spec che non carica sta in `errors`
        e NON in `warnings`, o un avviso finirebbe letto come un guasto."""
        self.assertIn("rotto", self.reg.errors())
        self.assertNotIn("rotto", self.reg.warnings())

    def test_a_second_load_does_not_accumulate(self) -> None:
        """`load()` ricostruisce: due reload di fila non devono raddoppiare gli
        avvisi, o la pagina mostrerebbe lo stesso difetto N volte."""
        prima = len(self.reg.warnings()["devseed"])
        self.reg.load()
        self.assertEqual(prima, len(self.reg.warnings()["devseed"]))

    def test_warnings_is_a_copy(self) -> None:
        """Come `errors()`: chi legge non deve poter mutare lo stato del registry."""
        self.reg.warnings()["devseed"] = ["inventato"]
        self.assertTrue(any("non ha modo di eseguire" in a
                            for a in self.reg.warnings()["devseed"]))


if __name__ == "__main__":
    unittest.main()
