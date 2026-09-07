"""Il transcript di un turno sopravvive allo spawn (clodia-platform#208).

Il file che dice PERCHÉ un turno ha fatto quello che ha fatto — prompt, testo,
ogni tool call — vive dentro lo spawn effimero, sotto
`.claude/projects/<slug>/<uuid>.jsonl`, e muore col reaper o con un restart. Il
caso della issue: quattro mattine di `success` su un job che non faceva niente,
tre cause diverse, e la spiegazione della seconda scritta in chiaro nel
transcript del primo run — letto mai, perché la cartella non c'era più.

Qui si verifica che una copia esca dallo spawn prima che lo spawn sparisca, e
che la copia non venga a sua volta persa dalla copia successiva.
"""
from __future__ import annotations

import ast
import json
import pathlib
import shutil
import tempfile
import unittest
from unittest.mock import patch

from . import transcripts


def _spawn(root: pathlib.Path, nome: str, uuid: str, righe: int = 3) -> pathlib.Path:
    """Uno spawn con dentro un transcript, nella forma vera: il runtime Claude
    scrive sotto `.claude/projects/<slug della cwd>/<uuid della sessione>.jsonl`."""
    d = root / nome
    proj = d / ".claude" / "projects" / f"-datadir-spawns-{nome}"
    proj.mkdir(parents=True)
    (proj / f"{uuid}.jsonl").write_text(
        "\n".join(json.dumps({"i": i, "type": "assistant"}) for i in range(righe)) + "\n",
        encoding="utf-8")
    return d


class TranscriptLeavesTheSpawnTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.dest = self.root / "transcripts"
        self._patch = patch.object(transcripts, "TRANSCRIPTS_DIR", self.dest)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_the_copy_outlives_the_spawn(self):
        spawn = _spawn(self.root, "clodia-1", "aaaa-1111")
        scritti = transcripts.persist("clodia", "chat-42", [spawn])
        self.assertEqual(len(scritti), 1)
        # Il reaper fa esattamente questo.
        shutil.rmtree(spawn)
        salvato = self.dest / "clodia" / "chat-42" / "aaaa-1111.jsonl"
        self.assertTrue(salvato.is_file())
        self.assertEqual(len(salvato.read_text(encoding="utf-8").strip().splitlines()), 3)

    def test_a_new_spawn_on_the_same_chat_does_not_overwrite_the_old_one(self):
        """Il motivo per cui la destinazione NON è `<chat_id>.jsonl`: una chat
        che ottiene un nuovo spawn (recovery, eviction) riparte da un file
        sorgente nuovo e più corto, e un file unico per chat farebbe sovrascrivere
        i turni di prima — quelli che la diagnosi cerca."""
        primo = _spawn(self.root, "clodia-1", "aaaa-1111", righe=5)
        transcripts.persist("clodia", "chat-42", [primo])
        secondo = _spawn(self.root, "clodia-2", "bbbb-2222", righe=1)
        transcripts.persist("clodia", "chat-42", [secondo])
        salvati = sorted(p.name for p in (self.dest / "clodia" / "chat-42").iterdir())
        self.assertEqual(salvati, ["aaaa-1111.jsonl", "bbbb-2222.jsonl"])

    def test_the_same_turn_copied_twice_is_just_updated(self):
        spawn = _spawn(self.root, "clodia-1", "aaaa-1111", righe=2)
        transcripts.persist("clodia", "chat-42", [spawn])
        # Il turno dopo: la sorgente è cresciuta (JSONL append-only).
        src = next((spawn / ".claude" / "projects").rglob("*.jsonl"))
        with src.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"i": 99}) + "\n")
        transcripts.persist("clodia", "chat-42", [spawn])
        salvato = self.dest / "clodia" / "chat-42" / "aaaa-1111.jsonl"
        self.assertEqual(len(salvato.read_text(encoding="utf-8").strip().splitlines()), 3)
        self.assertEqual(len(list((self.dest / "clodia" / "chat-42").iterdir())), 1)

    def test_a_chat_id_with_slashes_does_not_escape_the_destination(self):
        """I chat_id dei canali hanno la forma `chan:SEAL-1/software-house`: un
        path costruito con quello dentro finirebbe in una cartella annidata (o
        fuori, con un `..`). Il nome si sanifica."""
        spawn = _spawn(self.root, "clodia-1", "aaaa-1111")
        transcripts.persist("clodia", "chan:SEAL-1/../../etc", [spawn])
        scritti = [p for p in self.dest.rglob("*.jsonl")]
        self.assertEqual(len(scritti), 1)
        self.assertIn(self.dest, scritti[0].parents)

    def test_no_transcript_is_not_an_error(self):
        vuoto = self.root / "codex-1"
        (vuoto / "scratch").mkdir(parents=True)
        self.assertEqual(transcripts.persist("ophelia", "chat-1", [vuoto]), [])

    def test_an_unreadable_source_does_not_break_the_turn(self):
        """Best-effort è la sostanza, non la prudenza: un turno non deve morire
        perché una copia diagnostica è fallita."""
        spawn = _spawn(self.root, "clodia-1", "aaaa-1111")
        with patch.object(transcripts.shutil, "copyfile", side_effect=OSError("boom")):
            self.assertEqual(transcripts.persist("clodia", "chat-42", [spawn]), [])

    def test_persist_for_reads_the_spawn_dir_from_the_single_reader(self):
        """`spawn_dirs_of()` è l'unico lettore della dir di spawn, dopo il bug in
        cui due attributi divergevano: questa funzione non ne aggiunge un terzo."""
        spawn = _spawn(self.root, "clodia-1", "aaaa-1111")

        class _Ws:
            dir = spawn

        class _Chat:
            kind = "clodia"
            chat_id = "chat-7"
            _spawn = _Ws()

        self.assertEqual(len(transcripts.persist_for(_Chat())), 1)
        self.assertTrue((self.dest / "clodia" / "chat-7" / "aaaa-1111.jsonl").is_file())


class TheHookIsOnEveryOutcomeTests(unittest.TestCase):
    """Guard strutturale, nello stile di `test_no_sync_http_in_async_handlers`.

    Il punto d'aggancio è il `finally:` del turno, non il ramo che emette
    `run_done`: agganciato al successo, resterebbero fuori i turni cancellati,
    andati in timeout o uccisi dal watchdog — e nella #208 due dei tre casi
    osservati sono proprio turni appesi. Se qualcuno sposta la chiamata dentro
    il ramo felice, questo test lo dice.
    """

    def test_the_call_is_inside_a_finally_of_the_turn(self):
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "sdk_runtime" / "session.py")
        tree = ast.parse(src.read_text(encoding="utf-8"))

        def _chiama_persist(nodo) -> bool:
            # Si cerca il RIFERIMENTO `transcripts.persist_for`, non una
            # `ast.Call`: la chiamata vera passa da `asyncio.to_thread(...)` —
            # quel JSONL pesa MB e leggerlo sull'event loop fermerebbe il
            # processo — quindi la funzione è un argomento, non un callee.
            return any(isinstance(n, ast.Attribute)
                       and n.attr in ("persist_for", "persist")
                       and getattr(n.value, "id", "") == "transcripts"
                       for n in ast.walk(nodo))

        finally_con_persist = [
            t for t in ast.walk(tree)
            if isinstance(t, ast.Try) and t.finalbody
            and any(_chiama_persist(s) for s in t.finalbody)]
        self.assertTrue(finally_con_persist,
                        "nessun `finally:` di session.py persiste il transcript")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
