"""Un campo commentato da un marcatore è una dichiarazione che sparisce in silenzio.

clodia-platform#211, punto 3 — l'unico residuo dopo il censimento del 17 ago.

Il fatto: qualcosa ha riscritto i seed installati commentando campi VALIDI con un
marcatore macchina, `#v7compat#`, che non compare in nessun repository della
piattaforma. Su questa istanza erano 7 campi su 4 agenti, fra cui i `gated_tools`
di `fullstack-dev` — 15 verbi che il pack dichiara come «richiede consenso umano»
e che, commentati, non gattavano niente. Nessun errore, nessun log, nessuno stato
degradato: il seed carica, l'agente funziona, meno le dichiarazioni.

Il loader non può ripristinarli da sé (decidere per campo è una scelta umana, e
il censimento l'ha già fatta una volta), ma può smettere di non dirlo. Il canale
esiste già ed è `warnings()`, lo stesso di clodia-platform#227: il seed carica e
si contraddice — qui si contraddice col proprio pack.

Nota su cosa NON copre `extra="forbid"`: un campo *sconosciuto* fa già fallire il
parse e finisce in `errors()`. Il buco è esattamente l'opposto — un campo noto e
valido, tolto dalla vista mettendogli un `#` davanti. Per YAML non esiste; per chi
apre il file c'è ancora.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from .loader import AgentRegistry

# Il caso reale, ridotto: il seed di `fullstack-dev` come stava sull'istanza.
SEED_NEUTRALIZZATO = """\
name: devseed
display_name: Dev
description: d
model: claude-sonnet-4-5
system_prompt: system-prompt.md
native_tools: []
#v7compat# multi_spawn: true
#v7compat# max_spawns: 4
#v7compat# gated_tools:
#v7compat#   - topic.remote_write
"""

# Stesso seed, senza marcatori: i commenti in prosa e i separatori non sono
# dichiarazioni neutralizzate e non devono generare rumore.
SEED_PULITO = """\
name: lettore
display_name: Lettore
description: d
model: claude-sonnet-4-5
system_prompt: system-prompt.md
native_tools: []
# ── stack di inferenza ──────────────────────────────
# max_spawns qui non serve: l'agente è a spawn singolo
multi_spawn: false
"""


class NeutralisedFieldsAreReportedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        for nome, testo in (("devseed", SEED_NEUTRALIZZATO), ("lettore", SEED_PULITO)):
            d = base / nome
            d.mkdir()
            (d / "agent.yaml").write_text(testo)
        self.reg = AgentRegistry(base_dir=base)
        self.reg.load()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_neutralised_seed_is_in_warnings(self) -> None:
        self.assertIn("devseed", self.reg.warnings())

    def test_the_warning_names_the_marker(self) -> None:
        """Il marcatore è l'unica traccia di chi ha riscritto il file: se
        l'avviso non lo nomina, chi legge non ha da dove ripartire."""
        self.assertTrue(any("v7compat" in a for a in self.reg.warnings()["devseed"]),
                        self.reg.warnings())

    def test_the_warning_names_the_fields(self) -> None:
        """«Ci sono righe commentate» non è agibile: servono i nomi, perché la
        decisione si prende per campo — `gated_tools` non è `max_spawns`."""
        testo = " ".join(self.reg.warnings()["devseed"])
        for campo in ("multi_spawn", "max_spawns", "gated_tools"):
            self.assertIn(campo, testo)

    def test_a_list_item_counts_too(self) -> None:
        """`gated_tools` commentato porta con sé i suoi verbi: contare solo le
        righe `chiave:` direbbe «3 dichiarazioni» dove ce ne sono 4, e il verbo
        che il pack voleva gattare sparirebbe dal conto."""
        self.assertTrue(any("topic.remote_write" in a
                            for a in self.reg.warnings()["devseed"]),
                        self.reg.warnings())

    def test_prose_comments_are_not_flagged(self) -> None:
        """Nessuna chiave: un separatore e una nota in italiano non sono campi
        neutralizzati, e un avviso che grida a ogni commento non si legge più."""
        self.assertNotIn("lettore", self.reg.warnings())

    def test_the_seed_still_loads(self) -> None:
        """Si segnala, non si punisce: l'agente resta usabile (e i campi
        commentati restano inerti finché un umano non decide per ciascuno)."""
        self.assertIsNotNone(self.reg.get_by_name("devseed"))
        self.assertNotIn("devseed", self.reg.errors())

    def test_a_second_load_does_not_accumulate(self) -> None:
        prima = len(self.reg.warnings()["devseed"])
        self.reg.load()
        self.assertEqual(prima, len(self.reg.warnings()["devseed"]))


if __name__ == "__main__":
    unittest.main()
