"""La memoria persistente del seed deve arrivare nel prompt, e sopravvivere a #416.

clodia-platform#416 chiedeva di rimuovere il feedback 👍/👎 «end-to-end», e
metteva `prompt_section_for_spec` nell'inventario del codice morto da cancellare
insieme al resto. Non era morto: era l'unico punto che portava la `MEMORY.md` di
un agente dentro `system-prompt.md` — quindi non solo le lesson, ma l'indice di
memoria e il blocco dei documenti scritto da `memory.put_document`.

Il guasto che questi test esistono per impedire non è un errore: è un SILENZIO.
Eseguendo l'inventario alla lettera, ogni agente ripartirebbe senza la propria
memoria persistente, nessuna eccezione verrebbe sollevata e nessun test
esistente diventerebbe rosso. Da qui il primo test, che guarda il file prodotto
e non la funzione: se un domani l'iniezione sparisce di nuovo, questo fallisce.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from . import memory_prompt


def _spec(agent_dir: str):
    return SimpleNamespace(agent_dir=agent_dir, memory=SimpleNamespace(dir="memory/"))


def _scrivi_memoria(base: Path, testo: str) -> None:
    mem = base / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "MEMORY.md").write_text(testo, encoding="utf-8")


class SezioneDiPromptTests(unittest.TestCase):
    def test_la_memoria_del_seed_finisce_nella_sezione(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _scrivi_memoria(Path(tmp), "# Memory Index\n\n## Documenti\n\n- contratto.md\n")
            out = memory_prompt.prompt_section_for_spec(_spec(tmp))
        self.assertIn("## Memoria persistente del seed", out)
        self.assertIn("contratto.md", out)

    def test_il_blocco_del_feedback_rimosso_non_entra_nel_prompt(self) -> None:
        """Le `MEMORY.md` già sul disco portano il blocco gestito, ormai vuoto.
        Iniettarlo annuncerebbe a ogni agente una sezione «Lesson learned dal
        feedback umano» di un meccanismo che non esiste più."""
        with tempfile.TemporaryDirectory() as tmp:
            _scrivi_memoria(Path(tmp), (
                "# Memory Index\n\n"
                "<!-- clodia:feedback-lessons:start -->\n"
                "## Lesson learned dal feedback umano\n\n"
                "_Nessuna lesson appresa._\n"
                "<!-- clodia:feedback-lessons:end -->\n\n"
                "## Documenti\n\n- nota.md\n"
            ))
            out = memory_prompt.prompt_section_for_spec(_spec(tmp))
        self.assertNotIn("feedback-lessons", out)
        self.assertNotIn("Lesson learned", out)
        self.assertIn("nota.md", out)

    def test_una_memoria_solo_blocco_non_produce_una_sezione_vuota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _scrivi_memoria(Path(tmp), (
                "<!-- clodia:feedback-lessons:start -->\n"
                "_Nessuna lesson appresa._\n"
                "<!-- clodia:feedback-lessons:end -->\n"
            ))
            self.assertEqual(memory_prompt.prompt_section_for_spec(_spec(tmp)), "")

    def test_senza_memoria_non_si_inventa_una_sezione(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(memory_prompt.prompt_section_for_spec(_spec(tmp)), "")

    def test_leggere_non_scrive(self) -> None:
        """La vecchia strada riscriveva `MEMORY.md` a OGNI materializzazione di
        workspace per aggiornare il blocco gestito. Senza quel blocco, una
        scrittura per leggere è solo un effetto collaterale: qui si pretende che
        il file resti byte per byte quello di prima."""
        with tempfile.TemporaryDirectory() as tmp:
            testo = "# Memory Index\n\n## Documenti\n\n- nota.md\n"
            _scrivi_memoria(Path(tmp), testo)
            path = Path(tmp) / "memory" / "MEMORY.md"
            prima = path.stat().st_mtime_ns
            memory_prompt.prompt_section_for_spec(_spec(tmp))
            self.assertEqual(path.read_text(encoding="utf-8"), testo)
            self.assertEqual(path.stat().st_mtime_ns, prima)


class AggancioAlWorkspaceTests(unittest.TestCase):
    """Che la funzione esista non basta: deve essere CHIAMATA dal workspace.

    È l'anello che #416 stava per tagliare. Il controllo è sul sorgente perché
    materializzare un workspace vero richiede l'albero degli agent su
    `/datadir`, che qui non c'è: il limite è dichiarato, non nascosto.
    """

    def test_il_workspace_inietta_la_memoria(self) -> None:
        import inspect

        from . import workspace

        src = inspect.getsource(workspace.EphemeralWorkspace.create)
        self.assertIn("from .memory_prompt import prompt_section_for_spec", src)
        self.assertIn("memory_section", src)
        self.assertIn('(self.dir / "system-prompt.md").write_text(fused', src)


class FeedbackRimossoTests(unittest.TestCase):
    """Il meccanismo 👍/👎 non deve tornare per la porta di servizio."""

    def test_il_modulo_feedback_non_esiste_piu(self) -> None:
        with self.assertRaises(ImportError):
            from . import feedback  # noqa: F401

    def test_il_routing_feedback_e_un_altro_meccanismo_e_resta(self) -> None:
        """Nel codice convivevano due cose chiamate «feedback»: l'issue ne
        rimuove una sola, e questo test dice quale è rimasta."""
        from ..api import routing_feedback

        self.assertTrue(hasattr(routing_feedback, "record_feedback"))


if __name__ == "__main__":
    unittest.main()
