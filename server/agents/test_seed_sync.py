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

import pathlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from . import seed_sync
from . import seed_sync as S


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


class BackfillNewFieldsTests(unittest.TestCase):
    """Un campo che la copia locale non ha non è una modifica dell'owner.

    Il 12 ago `native_tools` è arrivato nel pack e sull'istanza i seed non
    l'avevano: la restrizione degli strumenti nativi era INERTE — `None` da tutte
    le parti, zero strumenti negati. La direzione d'errore giusta, ma una funzione
    di sicurezza che non fa niente e non lo dice è peggio di una assente.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pack = pathlib.Path(self.tmp) / "pack"
        self.data = pathlib.Path(self.tmp) / "data"
        (self.pack / "alfa").mkdir(parents=True)
        (self.data / "alfa").mkdir(parents=True)
        self.p_pack = self.pack / "alfa" / "agent.yaml"
        self.p_loc = self.data / "alfa" / "agent.yaml"
        self.ctx = [
            patch.object(S, "PACK_AGENTS_DIR", str(self.pack)),
            patch.object(S, "DATA_AGENTS_DIR", str(self.data)),
        ]
        for c in self.ctx:
            c.start()

    def tearDown(self):
        for c in self.ctx:
            c.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scrivi(self, pack: dict, locale: dict):
        self.p_pack.write_text(yaml.safe_dump(pack), encoding="utf-8")
        self.p_loc.write_text(yaml.safe_dump(locale), encoding="utf-8")

    def _locale(self) -> dict:
        return yaml.safe_load(self.p_loc.read_text(encoding="utf-8"))

    def test_a_missing_field_is_filled(self):
        self._scrivi({"name": "alfa", "native_tools": ["Read"]}, {"name": "alfa"})
        self.assertEqual(S.backfill_new_fields(), {"alfa": ["native_tools"]})
        self.assertEqual(self._locale()["native_tools"], ["Read"])

    def test_an_empty_declaration_is_not_touched(self):
        """`[]` è una dichiarazione dell'owner: «nessuno strumento nativo». È la
        differenza fra `[]` e assente, e tutta la ragione per cui questo backfill
        può esistere senza cancellare niente."""
        self._scrivi({"name": "alfa", "native_tools": ["Read", "Bash"]},
                     {"name": "alfa", "native_tools": []})
        self.assertEqual(S.backfill_new_fields(), {})
        self.assertEqual(self._locale()["native_tools"], [])

    def test_a_local_choice_is_not_overwritten(self):
        self._scrivi({"name": "alfa", "native_tools": ["Read", "Bash"]},
                     {"name": "alfa", "native_tools": ["Read"]})
        self.assertEqual(S.backfill_new_fields(), {})
        self.assertEqual(self._locale()["native_tools"], ["Read"])

    def test_the_declared_fields_are_all_restrictions(self):
        """L'elenco contiene solo campi che RESTRINGONO o dichiarano un vincolo.

        L'assenza di uno di quelli, nella copia locale, significa sempre «copiata
        prima che il vincolo esistesse» e mai «l'owner ha deciso di non averlo».
        Un campo che allarga non può entrare qui con la stessa leggerezza:
        riempirlo darebbe a un agente un potere che su quell'istanza nessuno gli
        ha dato.
        """
        self.assertEqual(set(S.BACKFILL_FIELDS),
                         {"native_tools", "denied_tools", "all_tier"})
        for allargante in ("tool_permissions", "capabilities", "providers",
                           "model", "clearance"):
            self.assertNotIn(allargante, S.BACKFILL_FIELDS)

    def test_denied_tools_reaches_a_seed_that_predates_it(self):
        """Il caso del 13 ago: A2 restringe messaggero con `denied_tools`, e la
        copia locale non ha quella chiave — senza backfill la PR resta inerte."""
        self._scrivi({"name": "alfa", "denied_tools": ["topic.read_file"]},
                     {"name": "alfa", "tool_permissions": ["email.*"]})
        self.assertEqual(S.backfill_new_fields(), {"alfa": ["denied_tools"]})
        self.assertEqual(self._locale()["denied_tools"], ["topic.read_file"])

    def test_only_the_declared_fields_travel(self):
        """L'elenco è chiuso di proposito: è la differenza fra «riempire un campo
        nuovo» e «aggiornare un seed», che resta la domanda aperta della #25."""
        self._scrivi({"name": "alfa", "model": "un-altro-modello",
                      "system_prompt": "diverso.md"},
                     {"name": "alfa"})
        self.assertEqual(S.backfill_new_fields(), {})
        loc = self._locale()
        self.assertNotIn("model", loc)
        self.assertNotIn("system_prompt", loc)

    def test_other_local_keys_survive_the_rewrite(self):
        self._scrivi({"name": "alfa", "native_tools": ["Read"]},
                     {"name": "alfa", "model": "scelto-a-mano",
                      "tool_permissions": ["topic.open"]})
        S.backfill_new_fields()
        loc = self._locale()
        self.assertEqual(loc["model"], "scelto-a-mano")
        self.assertEqual(loc["tool_permissions"], ["topic.open"])
        self.assertEqual(loc["native_tools"], ["Read"])

    def test_a_seed_absent_locally_is_left_to_sync_seeds(self):
        """Materializzare un seed che non c'è è il lavoro di `sync_seeds`. Qui si
        riempie un campo, e un campo non si riempie in un file che non esiste."""
        self.p_pack.write_text(yaml.safe_dump({"name": "alfa",
                                               "native_tools": ["Read"]}),
                               encoding="utf-8")
        # `setUp` non ha creato il file locale in questo caso: lo si rimuove solo
        # se c'è, perché il test descrive «il seed non è ancora materializzato».
        if self.p_loc.exists():
            self.p_loc.unlink()
        self.assertEqual(S.backfill_new_fields(), {})
