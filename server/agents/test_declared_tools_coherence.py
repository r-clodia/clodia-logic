"""Un seed che autorizza comandi senza `Bash` viene segnalato, non corretto.

clodia-platform#227. L'allowlist dei tool nativi (agents-notebook A9) è andata in
enforcement mentre nessun seed dei pack la dichiarava: sono caduti tutti sul
pavimento dell'arciseed, che non contiene `Bash`. `fullstack-dev` elencava otto
comandi in `allow_shell_cmds` e non aveva modo di eseguirne uno.

Il guasto non è che l'enforcement sbagli — è che **non fallisce**: nessuna
eccezione, nessun log, nessuno stato degradato. Un agente smette di poter fare
qualcosa e l'unico rilevatore è un umano che lo nota settimane dopo. Questo test
fissa il rilevatore meccanico.

`_incoerenze` SEGNALA e non corregge, di proposito: un seed contraddittorio non è
una ragione per fermare la colonia, e correggere in silenzio riprodurrebbe il
difetto in un altro punto.
"""
from __future__ import annotations

import unittest

from .loader import _incoerenze
from .models import AgentSpec


def _seed(**extra) -> AgentSpec:
    base = {"name": "prova", "description": "d", "display_name": "P",
            "model": "m", "system_prompt": "s.md"}
    return AgentSpec.model_validate({**base, **extra})


class ShellWithoutBashTests(unittest.TestCase):
    def test_commands_authorised_without_bash_are_reported(self) -> None:
        avvisi = _incoerenze(_seed(
            native_tools=["Grep"],
            sandbox={"allow_shell_cmds": ["git", "pytest", "npm", "node", "cargo"]}))
        self.assertTrue(any("non ha modo di eseguire" in a for a in avvisi), avvisi)

    def test_the_message_names_the_commands(self) -> None:
        """Un avviso che dice «incoerenza» e non quale manda a leggere il file:
        il messaggio deve poter essere agito da chi lo legge nei log."""
        avvisi = _incoerenze(_seed(
            native_tools=[], sandbox={"allow_shell_cmds": ["pytest"]}))
        self.assertTrue(any("pytest" in a for a in avvisi), avvisi)

    def test_bash_with_an_empty_shell_is_reported_too(self) -> None:
        """Il difetto opposto: una porta su una stanza senza niente dentro."""
        avvisi = _incoerenze(_seed(
            native_tools=["Bash"], sandbox={"allow_shell_cmds": []}))
        self.assertTrue(any("senza niente dentro" in a for a in avvisi), avvisi)

    def test_a_pattern_counts_as_bash(self) -> None:
        """`Bash(git:*)` concede `Bash` ritagliato: è la porta, e conta."""
        avvisi = _incoerenze(_seed(
            native_tools=["Bash(git:*)"], sandbox={"allow_shell_cmds": ["git"]}))
        self.assertEqual([], avvisi)

    def test_a_coherent_dev_seed_is_silent(self) -> None:
        self.assertEqual([], _incoerenze(_seed(
            native_tools=["Bash", "Grep", "Glob"],
            sandbox={"allow_shell_cmds": ["git", "pytest"]})))

    def test_a_coherent_reader_seed_is_silent(self) -> None:
        """Il caso `security-engineer`: nessuna shell per mandato, e nessun Bash."""
        self.assertEqual([], _incoerenze(_seed(
            native_tools=["Grep", "Glob"], sandbox={"allow_shell_cmds": []})))


class UndeclaredNativeToolsTests(unittest.TestCase):
    def test_an_undeclared_seed_is_reported(self) -> None:
        avvisi = _incoerenze(_seed(sandbox={"allow_shell_cmds": []}))
        self.assertTrue(any("non dichiarato" in a for a in avvisi), avvisi)

    def test_an_empty_declaration_is_silent(self) -> None:
        """`[]` è una DECISIONE («solo il pavimento»), `None` una dimenticanza —
        e la differenza fra le due è tutto il punto di questo controllo."""
        self.assertEqual([], _incoerenze(_seed(
            native_tools=[], sandbox={"allow_shell_cmds": []})))


if __name__ == "__main__":
    unittest.main()
