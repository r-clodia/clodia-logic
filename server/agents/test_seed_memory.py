"""La memoria del seed arriva nel prompt, e nessuno la cancella per sbaglio.

Il difetto che questi test descrivono non si è verificato, ed è per questo che
esistono: l'inventario di clodia-logic#416 elencava `prompt_section_for_spec`
fra il «codice morto collaterale» di `agents/feedback.py`, da rimuovere col
resto. Non era morta — `workspace.py` la chiama a ogni spawn, ed è l'unico
punto in cui `MEMORY.md` entra nel system prompt. Eseguito alla lettera, quel
pezzo dell'inventario avrebbe tolto a ogni agente della colonia la memoria
persistente: nessuna eccezione, nessun test rosso, solo agenti che non
ricordano più niente e nessun modo di accorgersene se non chiedendoglielo.

`IlPromptPortaLaMemoria` è la guardia contro il ritorno di quella rimozione.
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from . import seed_memory

_BLOCCO = (
    "<!-- clodia:feedback-lessons:start -->\n"
    "## Lesson learned dal feedback umano\n\n"
    "_Nessuna lesson appresa._\n"
    "<!-- clodia:feedback-lessons:end -->"
)


class _SpecFinta:
    """Il minimo che `prompt_section_for_spec` guarda: dove sta l'agente e come
    si chiama la sua cartella di memoria."""

    def __init__(self, agent_dir: str, mem_dir: str = "memory/"):
        self.agent_dir = agent_dir
        self.memory = SimpleNamespace(dir=mem_dir)


class LaSezioneDiPrompt(unittest.TestCase):
    def _agente(self, memory_md: str | None, mem_dir: str = "memory/"):
        base = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(base, ignore_errors=True))
        if memory_md is not None:
            path = Path(base) / mem_dir / "MEMORY.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(memory_md, encoding="utf-8")
        return _SpecFinta(base, mem_dir)

    def test_la_memoria_del_seed_finisce_nella_sezione(self):
        spec = self._agente("# Memory Index\n\nIl cliente Rossi paga a 60 giorni.\n")
        out = seed_memory.prompt_section_for_spec(spec)
        self.assertIn("Memoria persistente del seed", out)
        self.assertIn("Il cliente Rossi paga a 60 giorni.", out)

    def test_i_documenti_di_put_document_sono_memoria_quanto_le_note(self):
        """`memory.put_document` scrive la propria sezione dentro lo stesso
        `MEMORY.md`: se l'iniezione filtrasse per «note», i documenti
        sparirebbero dal prompt restando sul disco."""
        spec = self._agente(
            "# Memory Index\n\n<!-- clodia:documents:start -->\n"
            "## Documenti\n\n- perizia-2026.md\n"
            "<!-- clodia:documents:end -->\n"
        )
        self.assertIn("perizia-2026.md", seed_memory.prompt_section_for_spec(spec))

    def test_il_blocco_morto_del_feedback_non_entra_nel_prompt(self):
        """Le istanze già in giro hanno il blocco managed dentro `MEMORY.md`,
        col solo «_Nessuna lesson appresa._». Dopo il #416 è testo morto: va
        tolto dal prompt senza riscrivere il file di ogni agente."""
        spec = self._agente(f"# Memory Index\n\n{_BLOCCO}\n\nUna nota vera.\n")
        out = seed_memory.prompt_section_for_spec(spec)
        self.assertNotIn("clodia:feedback-lessons", out)
        self.assertNotIn("Nessuna lesson appresa", out)
        self.assertIn("Una nota vera.", out)

    def test_un_marcatore_senza_chiusura_non_mangia_la_memoria(self):
        """Taglio a occhio su un file malformato = memoria vera persa. Meglio
        lasciare tre righe morte che cancellarne trenta buone."""
        spec = self._agente("<!-- clodia:feedback-lessons:start -->\nUna nota vera.\n")
        self.assertIn("Una nota vera.", seed_memory.prompt_section_for_spec(spec))

    def test_niente_memoria_niente_sezione(self):
        """Un titolo con sotto il vuoto costa contesto per dire niente."""
        self.assertEqual(seed_memory.prompt_section_for_spec(self._agente(None)), "")
        self.assertEqual(seed_memory.prompt_section_for_spec(self._agente(f"{_BLOCCO}\n")), "")
        self.assertEqual(seed_memory.prompt_section_for_spec(None), "")

    def test_leggere_non_scrive(self):
        """Prima l'iniezione passava da `_sync_memory`, che riscriveva
        `MEMORY.md` a ogni spawn per rigenerare il blocco delle lesson. Senza
        feedback non c'è niente da sincronizzare: una lettura che scrive è un
        effetto collaterale che nessuno si aspetta da un prompt."""
        spec = self._agente("# Memory Index\n\nNota.\n")
        path = Path(spec.agent_dir) / "memory" / "MEMORY.md"
        prima = (path.read_text(encoding="utf-8"), os.stat(path).st_mtime_ns)
        seed_memory.prompt_section_for_spec(spec)
        self.assertEqual((path.read_text(encoding="utf-8"), os.stat(path).st_mtime_ns), prima)

    def test_un_agente_senza_file_di_memoria_non_lo_fa_nascere(self):
        spec = self._agente(None)
        seed_memory.prompt_section_for_spec(spec)
        self.assertFalse((Path(spec.agent_dir) / "memory" / "MEMORY.md").exists())


class IlPromptPortaLaMemoria(unittest.TestCase):
    """La guardia vera: che `workspace.py` la memoria la inietti davvero.

    Le funzioni qui sopra possono restare verdi per sempre mentre nessuno le
    chiama — è esattamente la forma del difetto di #419 (guard scritto, mai
    eseguito) e di ciò che #416 rischiava di fare qui.
    """

    def test_la_fusione_del_system_prompt_include_la_memoria_del_seed(self):
        sorgente = (Path(__file__).with_name("workspace.py")).read_text(encoding="utf-8")
        # `assertTrue` e non `assertIn`: su rosso `assertIn` stamperebbe
        # workspace.py per intero, e un fallimento illeggibile si impara a
        # ignorare come il rumore su stderr.
        self.assertTrue(
            "from .seed_memory import prompt_section_for_spec" in sorgente,
            "workspace.py non importa più l'iniezione della memoria del seed")
        fusione = sorgente[sorgente.index("parts = ["):]
        fusione = fusione[:fusione.index("\n\n")]
        self.assertTrue(
            "memory_section" in fusione,
            "la memoria del seed non entra più in system-prompt.md: il seed "
            "parte senza MEMORY.md e nessuno se ne accorge")

    def test_il_modulo_del_feedback_non_esiste_piu(self):
        """#416: se riapparisse, riapparirebbe anche la seconda copia
        dell'iniezione, e le due divergerebbero in silenzio."""
        self.assertFalse((Path(__file__).with_name("feedback.py")).exists())


if __name__ == "__main__":
    unittest.main()
