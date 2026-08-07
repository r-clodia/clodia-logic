"""Un ordinale di spawn non si riusa mai.

Voce 7: «i numeri già usati non sono riusabili in altri scope. Questo identifica
un carico di lavoro univocamente come `spawn-N` e finisce nell'audit trail».

Il difetto era registrato nella voce stessa e non ancora chiuso: l'indice era un
`max()` sulle **directory esistenti**, e il reaper cancella gli spawn vecchi.
Appena `clodia-124` spariva, il successivo riprendeva un numero già usato. marte
teneva 243 directory prima di una pulizia; dopo, la numerazione è ripartita
dentro terreno occupato.

Due carichi di lavoro con lo stesso nome in momenti diversi rendono una riga di
audit che dice `clodia-124` un'identificazione di niente — che è esattamente
ciò che quella riga serve a fare.

**Il pavimento alla prima esecuzione è la parte delicata.** Su un'istanza che già
gira il contatore non esiste ancora: ripartire da 1 riuserebbe in blocco tutti
gli ordinali vivi. Una migrazione che comincia sbagliando è peggio di nessuna
migrazione.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import workspace as W


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="spawn-seq-"))
        self.p = patch.object(W, "SPAWNS_ROOT", self.root)
        self.p.start()
        self.addCleanup(self.p.stop)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def dir_spawn(self, nome):
        (self.root / nome).mkdir(parents=True, exist_ok=True)

    def seq(self):
        f = self.root / W._SEQ_FILE
        return json.loads(f.read_text()) if f.is_file() else {}


class MonotonicTests(Base):
    def test_the_first_spawn_is_one(self):
        self.assertEqual(W._next_spawn_index("clodia"), 1)

    def test_it_increases(self):
        self.assertEqual([W._next_spawn_index("clodia") for _ in range(3)], [1, 2, 3])

    def test_deleting_a_spawn_does_not_free_its_number(self):
        """Il difetto che questo file esiste per chiudere."""
        n = W._next_spawn_index("clodia")
        self.dir_spawn(f"clodia-{n}")
        shutil.rmtree(self.root / f"clodia-{n}")      # il reaper
        self.assertEqual(W._next_spawn_index("clodia"), n + 1)

    def test_wiping_every_directory_does_not_reset_it(self):
        """Il caso di marte: 243 directory, una pulizia, e la numerazione che
        riparte dentro terreno occupato."""
        for _ in range(5):
            self.dir_spawn(f"clodia-{W._next_spawn_index('clodia')}")
        for d in list(self.root.iterdir()):
            if d.is_dir():
                shutil.rmtree(d)
        self.assertEqual(W._next_spawn_index("clodia"), 6)

    def test_each_seed_has_its_own_series(self):
        """`clodia-4` e `ophelia-4` possono coesistere: il contatore è per seed."""
        W._next_spawn_index("clodia")
        W._next_spawn_index("clodia")
        self.assertEqual(W._next_spawn_index("ophelia"), 1)
        self.assertEqual(W._next_spawn_index("clodia"), 3)


class MigrationTests(Base):
    def test_an_instance_already_running_starts_above_its_live_spawns(self):
        """Senza il pavimento, la prima allocazione dopo il deploy tornerebbe a 1
        e riuserebbe ogni ordinale vivo."""
        for n in (1, 2, 7):
            self.dir_spawn(f"clodia-{n}")
        self.assertEqual(W._next_spawn_index("clodia"), 8)

    def test_the_floor_only_counts_the_matching_seed(self):
        self.dir_spawn("ophelia-99")
        self.assertEqual(W._next_spawn_index("clodia"), 1)

    def test_a_directory_that_is_not_a_spawn_is_ignored(self):
        self.dir_spawn("clodia-non-un-numero")
        (self.root / "clodia-3").write_text("un file, non una dir")
        self.assertEqual(W._next_spawn_index("clodia"), 1)


class RobustnessTests(Base):
    def test_an_unreadable_counter_does_not_reset_the_series(self):
        """Ripartire da 1 riuserebbe ogni ordinale: si ricade sul pavimento, che
        è troppo basso ma mai più basso di così."""
        for _ in range(4):
            W._next_spawn_index("clodia")
        self.dir_spawn("clodia-4")
        (self.root / W._SEQ_FILE).write_text("{ rotto")
        self.assertEqual(W._next_spawn_index("clodia"), 5)

    def test_a_counter_that_is_not_a_mapping_is_ignored(self):
        (self.root / W._SEQ_FILE).write_text('["una lista"]')
        self.assertEqual(W._next_spawn_index("clodia"), 1)

    def test_the_counter_is_written_atomically(self):
        """Due spawn allocati insieme non devono trovare un file dimezzato."""
        import inspect
        src = inspect.getsource(W._next_spawn_index)
        self.assertIn(".replace(p)", src)

    def test_an_unwritable_counter_still_returns_an_ordinal(self):
        """Non poter salvare è un guasto da segnalare, non un motivo per non
        creare lo spawn."""
        with patch.object(Path, "write_text", side_effect=OSError("sola lettura")):
            self.assertIsInstance(W._next_spawn_index("clodia"), int)


if __name__ == "__main__":
    unittest.main()
