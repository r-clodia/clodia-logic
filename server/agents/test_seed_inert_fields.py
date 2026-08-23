"""Una dichiarazione commentata nel seed deve VEDERSI, non stare zitta.

clodia-platform#211, punto residuo. Qualcosa ha riscritto i seed installati
prefissando `#v7compat#` a sette campi validi di quattro agenti — fra cui i
`gated_tools`, il cui unico scopo è aggiungere un controllo umano. Nessun codice
e nessuna traccia: i campi erano inerti e l'istanza continuava a funzionare,
meno le dichiarazioni. Il rilevatore è stato un umano, un mese dopo.

Il difetto non è il marker: è che un campo neutralizzato non ha canale. Qui il
canale c'è già ed è `warnings()` (clodia-logic#310) — arriva all'API e alla
pagina. Questi test chiedono che una riga commentata che ASSEGNA un campo noto
finisca lì, con abbastanza dettaglio (campo, riga, marker) da poter agire.

Contro-prova indispensabile: i seed dei pack sono scritti con pagine di prosa
commentata in italiano, e un rilevatore che le segnala è un rilevatore che
nessuno leggerà più. Per questo si segnala solo un'ASSEGNAZIONE (`chiave:`) di
un campo che lo schema conosce e che il file non dichiara altrove.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from .loader import AgentRegistry

# Il caso reale, riga per riga come trovato sull'istanza il 17 ago 2026.
V7COMPAT = """\
name: devseed
display_name: Dev
description: d
model: claude-sonnet-4-5
system_prompt: system-prompt.md
native_tools: []
#v7compat# multi_spawn: true
#v7compat# max_spawns: 4
"""

# Neutralizzazione di una VOCE di lista, non di un campo: è la forma con cui è
# sparito un `gsheets.write_range` dai permessi di un altro seed.
VOCE_COMMENTATA = """\
name: listaseed
display_name: Lista
description: d
model: claude-sonnet-4-5
system_prompt: system-prompt.md
native_tools: []
tool_permissions:
  - topic.post_message
#v7compat#  - gsheets.write_range
"""

# Nessun marker: il commento a mano è la stessa inerzia senza la firma.
COMMENTATO_A_MANO = """\
name: manoseed
display_name: Mano
description: d
model: claude-sonnet-4-5
system_prompt: system-prompt.md
native_tools: []
# max_spawns: 4
"""

# Prosa: è come sono scritti i seed dei pack. Nessun avviso, o il canale annega.
PROSA = """\
name: prosaseed
display_name: Prosa
description: d
model: claude-sonnet-4-5
system_prompt: system-prompt.md
native_tools: []
# ANTENATO. `parents` è una relazione di AUTORITÀ: ciò che vi compare concede
# verbi per ereditarietà. Non è genealogia.
# NOTA: qui non si dichiara niente di eseguibile.
multi_spawn: true
# multi_spawn: true  ← documentato sopra, e DICHIARATO: non è inerte
"""

# Un principal umano non è eseguito, ma il suo seed si commenta allo stesso modo.
UMANO_NEUTRALIZZATO = """\
name: davide
display_name: Davide
description: d
type: human
#v7compat# telegram: "@davide"
"""

# Il caso peggiore misurato: 19 verbi commentati in un colpo (`impiegato-tomato`).
# Elencarli tutti fa un avviso lungo una pagina, e un avviso così viene scorso.
MOLTE_RIGHE = """\
name: moltoseed
display_name: Molto
description: d
model: claude-sonnet-4-5
system_prompt: system-prompt.md
native_tools: []
""" + "".join(f"#v7compat#  - verbo.numero_{i}\n" for i in range(19))

# Campo che lo schema non conosce: `extra="forbid"` lo rifiuta, ed è giusto —
# ma il messaggio deve dire cosa fare, o si finisce per commentarlo.
CAMPO_IGNOTO = """\
name: ignotoseed
display_name: Ignoto
description: d
model: claude-sonnet-4-5
system_prompt: system-prompt.md
native_tools: []
campo_che_non_esiste: 3
"""


class InertFieldsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        for nome, testo in (("devseed", V7COMPAT), ("listaseed", VOCE_COMMENTATA),
                            ("manoseed", COMMENTATO_A_MANO), ("prosaseed", PROSA),
                            ("davide", UMANO_NEUTRALIZZATO),
                            ("moltoseed", MOLTE_RIGHE),
                            ("ignotoseed", CAMPO_IGNOTO)):
            d = base / nome
            d.mkdir()
            (d / "agent.yaml").write_text(testo)
        self.reg = AgentRegistry(base_dir=base)
        self.reg.load()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _avvisi(self, nome: str) -> str:
        return "\n".join(self.reg.warnings().get(nome, []))

    def test_a_marked_field_is_reported(self) -> None:
        self.assertIn("devseed", self.reg.warnings())
        testo = self._avvisi("devseed")
        self.assertIn("multi_spawn", testo)
        self.assertIn("max_spawns", testo)

    def test_the_marker_is_named(self) -> None:
        """Chi legge deve poter fare `grep v7compat` sull'istanza: il marker è
        l'unica traccia di ciò che ha riscritto i seed."""
        self.assertIn("v7compat", self._avvisi("devseed"))

    def test_the_line_number_is_named(self) -> None:
        """Il campo va ritrovato nel file: `multi_spawn` è alla riga 7."""
        self.assertIn("7", self._avvisi("devseed"))

    def test_a_commented_list_item_is_reported(self) -> None:
        """Il marker basta: quello che neutralizza non è sempre un campo."""
        testo = self._avvisi("listaseed")
        self.assertIn("gsheets.write_range", testo)
        self.assertIn("v7compat", testo)

    def test_an_unmarked_commented_field_is_reported(self) -> None:
        self.assertIn("max_spawns", self._avvisi("manoseed"))

    def test_prose_is_not_reported(self) -> None:
        """La contro-prova: pagine di commenti in italiano, zero avvisi. Un campo
        citato in un commento E dichiarato nel file non è inerte."""
        self.assertNotIn("prosaseed", self.reg.warnings())

    def test_a_human_seed_is_checked_too(self) -> None:
        """`_incoerenze` salta chi non ha runtime, perché consiglia strumenti
        nativi. Un campo neutralizzato invece è muto su qualunque tipo."""
        self.assertIn("telegram", self._avvisi("davide"))

    def test_the_agent_stays_loaded(self) -> None:
        """Si segnala, non si punisce: l'avviso non toglie l'agente dalla lista."""
        self.assertIsNotNone(self.reg.get_by_name("devseed"))

    def test_a_second_load_does_not_accumulate(self) -> None:
        prima = len(self.reg.warnings()["devseed"])
        self.reg.load()
        self.assertEqual(prima, len(self.reg.warnings()["devseed"]))

    def test_many_lines_are_summarised_not_dumped(self) -> None:
        """Il totale è il dato che dice se andare a guardare il file; l'elenco
        integrale, a 19 righe, è ciò che fa scorrere l'avviso senza leggerlo."""
        testo = self._avvisi("moltoseed")
        self.assertIn("19 righe neutralizzate", testo)
        self.assertIn("e altre 11", testo)
        self.assertIn("verbo.numero_0", testo)
        self.assertNotIn("verbo.numero_18", testo)

    def test_an_unknown_field_error_says_not_to_comment_it(self) -> None:
        """`extra="forbid"` fa fallire il load, ed è il default sicuro. Ma il
        messaggio che ne esce è la ragione per cui qualcuno ha commentato invece
        di sistemare: deve nominare l'alternativa."""
        errore = self.reg.errors().get("ignotoseed", "")
        self.assertIn("campo_che_non_esiste", errore)
        self.assertIn("non commentarlo", errore.lower())
        self.assertIn("211", errore)


if __name__ == "__main__":
    unittest.main()
