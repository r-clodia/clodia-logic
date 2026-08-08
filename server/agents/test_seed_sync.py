"""I seed del pack arrivano sull'istanza.

La quarta sincronizzazione, e mancava. Skill, rule e costituzioni arrivano dal
base-pack; **i seed no** — la datadir viene popolata alla nascita e mai più.

Si è visto l'8 ago 2026 con l'arciseed: aggiunto al pack, mergiato, deployato, e
sull'istanza non c'era. Il gateway ha continuato a usare il pavimento di
bootstrap **e l'ha detto nel log**, che è l'unica ragione per cui ce ne siamo
accorti — un fallback silenzioso avrebbe lasciato credere che il seed fosse in
uso.

**Non sovrascrive quelli esistenti**, e questa è la parte delicata: un seed
materializzato può essere stato modificato dall'owner — verbi, provider, prompt —
e riscriverlo dal pack cancellerebbe una decisione presa. Quindi questo chiude
metà della #25: i seed NUOVI arrivano, l'aggiornamento di quelli esistenti resta
aperto, perché «quando una versione del pack deve prevalere su una modifica
locale» è una domanda di prodotto.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import seed_sync


class Base(unittest.TestCase):
    def setUp(self):
        self.pack = Path(tempfile.mkdtemp(prefix="pack-"))
        self.data = Path(tempfile.mkdtemp(prefix="data-"))
        self.addCleanup(shutil.rmtree, self.pack, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.data, ignore_errors=True)
        for p in (patch.object(seed_sync, "PACK_AGENTS_DIR", str(self.pack)),
                  patch.object(seed_sync, "DATA_AGENTS_DIR", str(self.data))):
            p.start()
            self.addCleanup(p.stop)

    def seed(self, nome, testo="name: x\n", dove=None):
        d = (dove or self.pack) / nome
        d.mkdir(parents=True, exist_ok=True)
        (d / "agent.yaml").write_text(testo)
        return d


class ArrivalTests(Base):
    def test_a_new_seed_is_materialised(self):
        self.seed("archseed")
        self.assertEqual(seed_sync.sync_seeds(), ["archseed"])
        self.assertTrue((self.data / "archseed" / "agent.yaml").is_file())

    def test_its_whole_directory_comes_along(self):
        """Un seed non è solo `agent.yaml`: porta prompt e memoria."""
        d = self.seed("archseed")
        (d / "system-prompt.md").write_text("ciao")
        seed_sync.sync_seeds()
        self.assertTrue((self.data / "archseed" / "system-prompt.md").is_file())

    def test_an_existing_seed_is_left_alone(self):
        """Può essere stato modificato dall'owner, e riscriverlo dal pack
        cancellerebbe una decisione presa."""
        self.seed("clodia", "name: dal-pack\n")
        self.seed("clodia", "name: modificato-in-locale\n", dove=self.data)
        seed_sync.sync_seeds()
        self.assertIn("modificato-in-locale",
                      (self.data / "clodia" / "agent.yaml").read_text())

    def test_only_directories_with_a_seed_file_count(self):
        (self.pack / "non-un-seed").mkdir()
        self.assertEqual(seed_sync.sync_seeds(), [])


class RobustnessTests(Base):
    def test_a_missing_pack_does_not_raise(self):
        """Un'istanza senza pack bundled non deve fallire il boot."""
        with patch.object(seed_sync, "PACK_AGENTS_DIR", str(self.pack / "assente")):
            self.assertEqual(seed_sync.sync_seeds(), [])

    def test_one_bad_seed_does_not_stop_the_others(self):
        self.seed("buono")
        self.seed("cattivo")
        vero = shutil.copytree

        def rotto(src, dst, *a, **k):
            if str(src).endswith("cattivo"):
                raise OSError("disco pieno")
            return vero(src, dst, *a, **k)

        with patch.object(seed_sync.shutil, "copytree", rotto):
            self.assertEqual(seed_sync.sync_seeds(), ["buono"])


class BootTests(unittest.TestCase):
    def test_it_runs_before_anything_can_spawn(self):
        """Uno spawn è fatto da un seed: materializzarli dopo significherebbe
        poter nascere senza."""
        import inspect
        from .. import main as M
        src = inspect.getsource(M)
        self.assertIn("sync_seeds", src)


if __name__ == "__main__":
    unittest.main()
