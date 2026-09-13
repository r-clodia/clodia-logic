"""Lo stderr del CLI Claude dice DI CHI è, o non serve a diagnosticare nessuno.

clodia-logic#420. Il Python SDK pipa lo stderr del sottoprocesso **solo se chi
lo apre registra un callback**: in `subprocess_cli.py` la destinazione è
`PIPE if self._options.stderr is not None else None`. Senza callback il
sottoprocesso eredita il file descriptor del container, e lo stderr di ogni
sessione di ogni agente finisce nello stesso posto, mescolato e anonimo: si
vede che «qualcosa» ha scritto, non quale agente né in quale chat. Diagnosticare
per agente diventa archeologia.

Il pattern per uscirne esiste già in questo stesso file, scritto per opencode:
le righe vanno nel logger **e** in una coda corta, che poi accompagna il
prossimo errore — «controlla i log del server» è un consiglio inutile se quei
log li stiamo buttando via. Qui la stessa coppia serve il ramo Claude, e il
test statico in fondo è quello che conta davvero: il difetto della #420 **è**
l'assenza della chiave `stderr` fra le opzioni, e nessun test funzionale sul
callback se ne accorgerebbe finché nessuno lo registra.

Livello di log: `INFO`, non `DEBUG` come per opencode. Oggi queste righe
esistono (grezze, nello stdout del container); instradarle a un livello spento
di default le farebbe *sparire* — un fix che toglie informazione non è un fix.
"""
from __future__ import annotations

import ast
import logging
import pathlib
import unittest
from collections import deque

from . import session as S


class TheSinkSaysWhoWroteTheLineTests(unittest.TestCase):
    """Una riga senza identità è il difetto: il tag è tutto il valore."""

    def test_the_line_is_logged_with_kind_and_chat(self):
        coda: deque = deque(maxlen=S._CLI_STDERR_TAIL)
        sink = S._cli_stderr_sink("avvocato", "chat-7", coda)
        with self.assertLogs(S.LOG, level="INFO") as log:
            sink("qualcosa è andato storto")
        riga = "\n".join(log.output)
        self.assertIn("avvocato", riga)
        self.assertIn("chat-7", riga)
        self.assertIn("qualcosa è andato storto", riga)

    def test_the_line_is_kept_for_the_next_error(self):
        coda: deque = deque(maxlen=S._CLI_STDERR_TAIL)
        sink = S._cli_stderr_sink("clodia", "chat-1", coda)
        sink("boom")
        self.assertEqual(list(coda), ["boom"])

    def test_a_very_long_line_does_not_become_the_log(self):
        """Un tool che vomita 2 MB su stderr non deve riscrivere il log del
        container: la riga intera resta nella coda, il logger ne prende un
        pezzo — stessa misura già scelta per opencode."""
        coda: deque = deque(maxlen=S._CLI_STDERR_TAIL)
        sink = S._cli_stderr_sink("clodia", "chat-1", coda)
        with self.assertLogs(S.LOG, level="INFO") as log:
            sink("x" * 5000)
        self.assertLess(len(log.output[0]), 1000)

    def test_blank_lines_are_not_events(self):
        coda: deque = deque(maxlen=S._CLI_STDERR_TAIL)
        sink = S._cli_stderr_sink("clodia", "chat-1", coda)
        logging.disable(logging.NOTSET)
        sink("")
        sink("   ")
        self.assertEqual(list(coda), [])

    def test_the_callback_never_raises(self):
        """Il transport isola già le eccezioni del callback, ma un sink che
        solleva su una riga malformata farebbe dipendere la sessione dalla
        cortesia di una libreria di terze parti."""
        sink = S._cli_stderr_sink("clodia", "chat-1", None)  # type: ignore[arg-type]
        sink("riga con una coda che non esiste")  # non solleva


class TheTailIsShortAndAccompaniesTheErrorTests(unittest.TestCase):
    """La coda è diagnosi, non un log: poche righe, le ultime."""

    def test_the_tail_forgets_the_oldest(self):
        coda: deque = deque(maxlen=S._CLI_STDERR_TAIL)
        sink = S._cli_stderr_sink("clodia", "chat-1", coda)
        for i in range(S._CLI_STDERR_TAIL + 60):
            sink(f"r{i}")
        self.assertEqual(len(coda), S._CLI_STDERR_TAIL)
        self.assertEqual(coda[-1], f"r{S._CLI_STDERR_TAIL + 59}")

    def test_the_hint_carries_the_last_lines(self):
        coda: deque = deque(["a", "b", "c"])
        hint = S._stderr_hint_from(coda)
        self.assertIn("stderr", hint)
        self.assertIn("c", hint)

    def test_no_lines_no_hint(self):
        """Un errore che non ha nulla da aggiungere non aggiunge nulla."""
        self.assertEqual(S._stderr_hint_from(deque()), "")

    def test_opencode_keeps_its_hint_unchanged(self):
        """Il ramo opencode deve continuare a rispondere come prima: questa è
        una generalizzazione, non un secondo meccanismo parallelo."""
        sessione = S.OpenCodeChatSession.__new__(S.OpenCodeChatSession)
        sessione._stderr_tail = deque(["prima", "dopo"])
        self.assertEqual(sessione._stderr_hint(), S._stderr_hint_from(deque(["prima", "dopo"])))


class TheOptionsActuallyCarryTheCallbackTests(unittest.TestCase):
    """Il test che sarebbe stato rosso per tutta la vita della #420.

    Tutto il resto di questo file può passare mentre il difetto è intatto: se
    `ChatSession.start()` non mette la chiave `stderr` fra le opzioni, l'SDK
    non apre nessuna pipe e il sink più curato del mondo non riceve una riga.
    Guarda l'albero e non il testo del file, perché `start` compare tre volte
    qui dentro e una ricerca testuale prenderebbe la prima che capita — è già
    successo, ed è il motivo per cui i test di `test_opencode_stderr` guardano
    l'AST.
    """

    def _start_di(self, classe: str) -> str:
        src = pathlib.Path(__file__).with_name("session.py").read_text(encoding="utf-8")
        for nodo in ast.parse(src).body:
            if isinstance(nodo, ast.ClassDef) and nodo.name == classe:
                start = next(m for m in nodo.body
                             if isinstance(m, ast.AsyncFunctionDef) and m.name == "start")
                return ast.unparse(start)
        self.fail(f"{classe} non trovata")

    def test_claude_registers_the_stderr_callback(self):
        self.assertIn("opts_kwargs['stderr']", self._start_di("ChatSession"))

    def test_the_error_the_user_sees_can_carry_the_stderr(self):
        """La coda esiste per essere letta: se nessuno la legge, è un secondo
        log che nessuno guarda."""
        src = pathlib.Path(__file__).with_name("session.py").read_text(encoding="utf-8")
        for nodo in ast.parse(src).body:
            if isinstance(nodo, ast.ClassDef) and nodo.name == "ChatSession":
                pub = next(m for m in nodo.body
                           if isinstance(m, ast.AsyncFunctionDef) and m.name == "_publish_error")
                self.assertIn("_stderr_hint_from", ast.unparse(pub))
                return
        self.fail("ChatSession non trovata")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
