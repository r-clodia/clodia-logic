"""I campi `sandbox` sono applicati SOLO su claude, e il seed non lo diceva.

`workspace._build_settings_json` traduce `allow_read`/`deny_read`/`allow_write`/
`allow_shell_cmds`/`deny_shell_patterns` in `.claude/settings.local.json`, ed è
chiamato solo dal layout claude. Su codex e opencode nessuno li porta: `ophelia`
dichiara cinque comandi ammessi e tre pattern negati, `messaggero` e
`segretario` un pattern negato ciascuno, e nessuno dei tre elenchi arriva al
runtime (clodia-platform#296).

La lettura di quei campi però c'è, ed è lettura per RACCONTARE: la scheda
dell'agente li mostra e il punteggio trifecta li interroga. Un campo dichiarato e
non portato è peggio di un campo assente, perché chi legge la scheda vede una
restrizione che non esiste — è lo stesso difetto che `native_tools_info.unenforced`
ha risolto per gli strumenti nativi, e qui si applica lo stesso pattern.

Cosa si misura:
- la tabella dice il vero, e lo dice per CAMPO dichiarato (un campo vuoto non
  racconta niente, quindi non si nomina);
- l'incoerenza esce da dove si legge il seed: gli avvisi del loader (che dalla
  #227 viaggiano con la scheda) e `sandbox_info` nella scheda stessa;
- la premessa della tabella resta vera: se domani qualcuno applica il sandbox su
  un altro runtime, è il test a diventare rosso invece della tabella a mentire.
"""
from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from ..sdk_runtime import native_tools as nt
from .loader import AgentRegistry
from .models import Sandbox

#: Come `ophelia`: codex, cinque comandi ammessi e tre pattern negati.
OPHELIA_LIKE = """\
name: codexseed
display_name: Codexseed
description: d
model: gpt-5
agent_sdk: codex
system_prompt: system-prompt.md
native_tools: ["Bash", "Read"]
sandbox:
  allow_write: ["{scratch}/**"]
  allow_shell_cmds: ["git", "pytest", "ls", "cat", "grep"]
  deny_shell_patterns: ["rm -rf *", "curl *", "sudo *"]
"""

#: Lo STESSO seed su claude: qui i campi si applicano, e non c'è niente da dire.
CLAUDE_LIKE = """\
name: claudeseed
display_name: Claudeseed
description: d
model: claude-sonnet-4-5
agent_sdk: claude
system_prompt: system-prompt.md
native_tools: ["Bash", "Read"]
sandbox:
  allow_write: ["{scratch}/**"]
  allow_shell_cmds: ["git", "pytest", "ls", "cat", "grep"]
  deny_shell_patterns: ["rm -rf *", "curl *", "sudo *"]
"""

#: Come `messaggero`: opencode, un solo pattern negato e nessun comando ammesso.
MESSAGGERO_LIKE = """\
name: opencodeseed
display_name: Opencodeseed
description: d
model: gpt-oss-120b
agent_sdk: opencode
system_prompt: system-prompt.md
native_tools: ["Bash"]
sandbox:
  deny_shell_patterns: ["sudo *"]
"""

#: Un seed codex che NON dichiara niente nel sandbox: non c'è incoerenza da
#: segnalare, e un avviso qui sarebbe rumore su ogni load.
CODEX_MUTO = """\
name: codexmuto
display_name: Codexmuto
description: d
model: gpt-5
agent_sdk: codex
system_prompt: system-prompt.md
native_tools: ["Read"]
"""


def _sandbox(**kw) -> Sandbox:
    return Sandbox(**kw)


class TheTableSaysWhatEachRuntimeApplies(unittest.TestCase):

    def test_codex_carries_none_of_the_declared_fields(self):
        residuo = nt.sandbox_unenforced("codex", _sandbox(
            allow_shell_cmds=["git", "pytest", "ls", "cat", "grep"],
            deny_shell_patterns=["rm -rf *", "curl *", "sudo *"]))
        self.assertEqual(residuo, ["allow_shell_cmds", "deny_shell_patterns"])

    def test_opencode_carries_none_either(self):
        residuo = nt.sandbox_unenforced("opencode",
                                        _sandbox(deny_shell_patterns=["sudo *"]))
        self.assertEqual(residuo, ["deny_shell_patterns"])

    def test_claude_carries_them_all(self):
        residuo = nt.sandbox_unenforced("claude", _sandbox(
            allow_read=["/x/**"], deny_read=["/y/**"], allow_write=["{scratch}/**"],
            allow_shell_cmds=["git"], deny_shell_patterns=["sudo *"]))
        self.assertEqual(residuo, [])

    def test_an_empty_field_is_never_named(self):
        """Un campo non dichiarato non racconta niente: nominarlo sposterebbe
        l'avviso dall'incoerenza al rumore, su ogni seed non-claude."""
        self.assertEqual(nt.sandbox_unenforced("codex", _sandbox()), [])

    def test_deny_read_is_in_the_same_boat(self):
        """Quinto campo, stessa sorte: la guardia sta nel punto condiviso, non
        sui due nomi che l'issue cita."""
        self.assertEqual(nt.sandbox_unenforced("codex", _sandbox(deny_read=["/etc/**"])),
                         ["deny_read"])

    def test_an_unknown_runtime_applies_nothing(self):
        """Direzione prudente, come `unenforced_denied`: di un runtime che non
        conosciamo non si può affermare che porti la restrizione."""
        self.assertEqual(
            nt.sandbox_unenforced("qualcosa-di-nuovo", _sandbox(allow_shell_cmds=["git"])),
            ["allow_shell_cmds"])

    def test_no_sandbox_at_all_is_not_a_crash(self):
        self.assertEqual(nt.sandbox_unenforced("codex", None), [])


class TheSeedSaysItWhereItIsRead(unittest.TestCase):
    """Gli avvisi del loader: dalla #227 viaggiano con la scheda dell'agente,
    quindi sono il canale che rende l'incoerenza visibile a una persona."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        for nome, testo in (("codexseed", OPHELIA_LIKE), ("claudeseed", CLAUDE_LIKE),
                            ("opencodeseed", MESSAGGERO_LIKE), ("codexmuto", CODEX_MUTO)):
            d = base / nome
            d.mkdir()
            (d / "agent.yaml").write_text(testo)
        self.reg = AgentRegistry(base_dir=base)
        self.reg.load()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _avvisi(self, nome: str) -> list[str]:
        return self.reg.warnings().get(nome, [])

    def test_a_codex_seed_is_told_its_sandbox_is_inert(self):
        avvisi = self._avvisi("codexseed")
        self.assertTrue(any("codex" in a and "allow_shell_cmds" in a for a in avvisi),
                        avvisi)

    def test_the_warning_names_the_fields_and_how_many(self):
        """«non applicato» senza dire cosa non è applicato non è agibile: chi
        legge deve sapere quali elenchi sono finti e quanto grandi."""
        avvisi = " · ".join(self._avvisi("codexseed"))
        self.assertIn("deny_shell_patterns", avvisi)
        self.assertIn("allow_write", avvisi)

    def test_an_opencode_seed_too(self):
        avvisi = self._avvisi("opencodeseed")
        self.assertTrue(any("deny_shell_patterns" in a and "opencode" in a
                            for a in avvisi), avvisi)

    def test_the_same_seed_on_claude_says_nothing(self):
        """Il controllo deve dipendere dal RUNTIME, non dai campi: su claude gli
        stessi elenchi sono applicati e un avviso sarebbe una bugia opposta."""
        avvisi = " · ".join(self._avvisi("claudeseed"))
        self.assertNotIn("allow_shell_cmds", avvisi)

    def test_a_codex_seed_without_sandbox_has_no_entry(self):
        self.assertNotIn("codexmuto", self.reg.warnings())

    def test_the_bash_advice_is_not_given_where_it_cannot_be_taken(self):
        """L'avviso «`Bash` concesso ma `allow_shell_cmds` è vuoto: una porta su
        una stanza senza niente dentro» consiglia di riempire un elenco che su
        codex/opencode nessuno legge: un consiglio impossibile da agire è la
        stessa forma di difetto dell'issue."""
        avvisi = " · ".join(self._avvisi("opencodeseed"))
        self.assertNotIn("porta su una stanza", avvisi)


class TheAgentCardCarriesIt(unittest.TestCase):
    """Il canale strutturato, gemello di `native_tools_info`: la scheda dice cosa
    è dichiarato E quanto ne è applicato, sempre, anche quando è tutto."""

    def _info(self, testo: str) -> dict:
        from ..api.agent_registry import _sandbox_info
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            nome = [r.split(":", 1)[1].strip()
                    for r in testo.splitlines() if r.startswith("name:")][0]
            (base / nome).mkdir()
            (base / nome / "agent.yaml").write_text(testo)
            reg = AgentRegistry(base_dir=base)
            reg.load()
            return _sandbox_info(reg.get_by_name(nome))

    def test_the_card_names_the_unenforced_fields(self):
        info = self._info(OPHELIA_LIKE)
        self.assertEqual(info["unenforced"],
                         ["allow_write", "allow_shell_cmds", "deny_shell_patterns"])

    def test_the_card_still_reports_the_declaration(self):
        """`declared` resta: il campo va mostrato, con accanto il fatto che non
        conta. Nasconderlo sarebbe la bugia opposta."""
        info = self._info(OPHELIA_LIKE)
        self.assertEqual(info["declared"]["allow_shell_cmds"],
                         ["git", "pytest", "ls", "cat", "grep"])

    def test_on_claude_the_residue_is_empty(self):
        self.assertEqual(self._info(CLAUDE_LIKE)["unenforced"], [])


class TheTablePremiseStaysTrue(unittest.TestCase):
    """La tabella dice «solo claude» perché una sola funzione applica quei campi
    e la chiama un solo layout. Se domani l'enforcement arriva su opencode, è
    QUESTO test a diventare rosso: senza, la tabella mentirebbe al contrario e
    la scheda dichiarerebbe inerte una restrizione reale."""

    def test_build_settings_json_is_called_only_by_the_claude_layout(self):
        src = (Path(__file__).resolve().parent / "workspace.py").read_text(encoding="utf-8")
        albero = ast.parse(src)
        chiamanti: set[str] = set()
        for nodo in ast.walk(albero):
            if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for interno in ast.walk(nodo):
                if (isinstance(interno, ast.Call)
                        and isinstance(interno.func, ast.Name)
                        and interno.func.id == "_build_settings_json"):
                    chiamanti.add(nodo.name)
        self.assertEqual(chiamanti, {"_materialize_claude_layout"},
                         "il sandbox è applicato da un layout nuovo: aggiorna "
                         "SANDBOX_ENFORCED in sdk_runtime/native_tools.py")


if __name__ == "__main__":
    unittest.main()
