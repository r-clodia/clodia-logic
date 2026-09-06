"""Il seed installato è ancora quello del pack? (clodia-platform#266)

Punto 4 della #211, l'ultimo rimasto. La #343 rileva ciò che è **spento dentro
il file locale** — una riga commentata che il seed non dichiara più. Non risponde
alla domanda diversa e più larga: *la copia installata corrisponde a quella del
pack?*

Un campo **cancellato del tutto** non lascia traccia: nessun commento da
segnalare, nessun errore di parse, il file semplicemente non lo dichiara. Per la
#343 quel seed è pulito. E un pack update lo fa tornare in vita — con
`gated_tools` significa un verbo che ricomincia (o smette) di chiedere un
consenso, per un aggiornamento fatto per altro.

Il confronto sta sul PARSATO, non sul testo: i seed dei pack hanno pagine di
prosa commentata in italiano e un diff testuale sarebbe rumore. Che è anche il
motivo per cui questo prende il caso che la regex della #343 non prende — una
chiave di lista commentata senza valore sulla riga (`#   gated_tools:`): sparita
dal parsato è sparita, la forma del commento non c'entra.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from . import seed_sync


def _dump(**campi) -> str:
    return yaml.safe_dump(campi, allow_unicode=True, sort_keys=False)


class Base(unittest.TestCase):
    def setUp(self):
        self.pack = Path(tempfile.mkdtemp(prefix="pack-"))
        self.data = Path(tempfile.mkdtemp(prefix="data-"))
        self.addCleanup(shutil.rmtree, self.pack, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.data, ignore_errors=True)

    def seed(self, nome: str, testo: str, dove: Path | None = None) -> Path:
        d = (dove or self.pack) / nome
        d.mkdir(parents=True, exist_ok=True)
        (d / "agent.yaml").write_text(testo, encoding="utf-8")
        return d

    def drift(self) -> list[dict]:
        return seed_sync.seed_drift(
            sorted(p for p in self.pack.iterdir() if p.is_dir()), self.data)

    def per_agente(self, nome: str) -> dict:
        for voce in self.drift():
            if voce["name"] == nome:
                return voce
        self.fail(f"nessuna voce di drift per '{nome}'")


class CampoRimossoTests(Base):
    """Il caso della issue: il campo non c'è più, e niente lo dice."""

    def test_a_field_deleted_locally_is_reported_missing(self):
        self.seed("impiegato", _dump(name="impiegato", gated_tools=["gsheets.write_range"]))
        self.seed("impiegato", _dump(name="impiegato"), dove=self.data)
        voce = self.per_agente("impiegato")
        self.assertEqual(voce["missing"], ["gated_tools"])
        self.assertEqual(voce["changed"], [])

    def test_a_commented_out_list_key_is_reported_too(self):
        """`#   gated_tools:` — chiave di lista commentata, niente dopo i due
        punti: la forma che `_CAMPO_COMMENTATO` della #343 NON vede, ed era la
        più dannosa del censimento reale (19 verbi sotto una chiave sola)."""
        self.seed("impiegato", _dump(name="impiegato", gated_tools=["a", "b"]))
        self.seed("impiegato",
                  "name: impiegato\n#   gated_tools:\n#     - a\n#     - b\n",
                  dove=self.data)
        self.assertEqual(self.per_agente("impiegato")["missing"], ["gated_tools"])


class ValoreCambiatoTests(Base):
    def test_a_changed_scalar_names_both_sides(self):
        self.seed("clodia", _dump(name="clodia", agent_sdk="claude"))
        self.seed("clodia", _dump(name="clodia", agent_sdk="opencode"), dove=self.data)
        cambiato = self.per_agente("clodia")["changed"]
        self.assertEqual([c["field"] for c in cambiato], ["agent_sdk"])
        self.assertIn("claude", cambiato[0]["pack"])
        self.assertIn("opencode", cambiato[0]["local"])

    def test_a_changed_list_says_which_entries_moved(self):
        """Su una lista di 19 verbi il valore intero non si legge: quello che
        serve è quali voci mancano e quali sono in più."""
        self.seed("fullstack", _dump(name="fullstack", gated_tools=["a", "b", "c"]))
        self.seed("fullstack", _dump(name="fullstack", gated_tools=["a", "d"]),
                  dove=self.data)
        cambiato = self.per_agente("fullstack")["changed"][0]
        self.assertEqual(cambiato["field"], "gated_tools")
        self.assertEqual(cambiato["removed"], ["b", "c"])
        self.assertEqual(cambiato["added"], ["d"])

    def test_list_order_alone_is_not_drift(self):
        """Riordinare `native_tools` non cambia cosa l'agente può fare: un
        rilevatore che segnala un riordino è un rilevatore che nessuno rilegge."""
        self.seed("clodia", _dump(name="clodia", native_tools=["Bash", "Grep"]))
        self.seed("clodia", _dump(name="clodia", native_tools=["Grep", "Bash"]),
                  dove=self.data)
        self.assertEqual(self.drift(), [])


class SilenzioTests(Base):
    def test_identical_seeds_produce_nothing(self):
        testo = _dump(name="clodia", gated_tools=["a"], native_tools=[])
        self.seed("clodia", testo)
        self.seed("clodia", testo, dove=self.data)
        self.assertEqual(self.drift(), [])

    def test_comments_and_key_order_are_not_drift(self):
        """Il confronto è sul parsato: la prosa commentata dei seed del pack e
        l'ordine delle chiavi non sono divergenze."""
        self.seed("clodia", "# una pagina di prosa\nname: clodia\ntype: normal\n")
        self.seed("clodia", "type: normal\nname: clodia\n", dove=self.data)
        self.assertEqual(self.drift(), [])


class CampoSoloLocaleTests(Base):
    def test_a_local_only_field_is_extra_not_missing(self):
        """Non è un difetto — è una cosa da sapere, e sta in un canale suo:
        confonderla con un campo perso direbbe il contrario del vero."""
        self.seed("clodia", _dump(name="clodia"))
        self.seed("clodia", _dump(name="clodia", telegram="@qualcuno"), dove=self.data)
        voce = self.per_agente("clodia")
        self.assertEqual(voce["extra"], ["telegram"])
        self.assertEqual(voce["missing"], [])
        self.assertEqual(voce["changed"], [])


class RobustezzaTests(Base):
    def test_a_seed_not_installed_is_said_so(self):
        self.seed("nuovo", _dump(name="nuovo"))
        voce = self.per_agente("nuovo")
        self.assertFalse(voce["installed"])
        self.assertEqual(voce["missing"], [])

    def test_an_unreadable_local_seed_does_not_stop_the_others(self):
        self.seed("rotto", _dump(name="rotto"))
        self.seed("rotto", "name: [rotto\n", dove=self.data)
        self.seed("sano", _dump(name="sano", gated_tools=["a"]))
        self.seed("sano", _dump(name="sano"), dove=self.data)
        self.assertTrue(self.per_agente("rotto")["error"])
        self.assertEqual(self.per_agente("sano")["missing"], ["gated_tools"])

    def test_a_directory_without_agent_yaml_is_skipped(self):
        (self.pack / "non-un-seed").mkdir()
        self.assertEqual(self.drift(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
