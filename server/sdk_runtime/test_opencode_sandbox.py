"""Il `deny_shell_patterns` del seed arriva a opencode (clodia-platform#296 pt.2).

Fino a qui i campi `sandbox` li traduceva solo claude: `ophelia` dichiarava tre
pattern negati, `messaggero` e `segretario` uno ciascuno, e nessuno arrivava al
runtime. Il punto 1 dell'issue (che la piattaforma DICA di non applicarli) è
chiuso; questo è il punto 2, sul solo runtime che ha un canale verificabile.

## Cosa si misura, e perché queste tre cose e non altre

- **`deny_shell_patterns` esce in `permission.bash`**, che è il solo posto dove
  opencode sa leggere una restrizione sulla shell;
- **l'ordine**, che qui non è cosmetica: opencode risolve una regola con
  `findLast(...)` — misurato sul binario 1.15.13, `findLast((X) => match($,
  X.permission) && match(Z, X.pattern)) ?? {action: "ask"}` — quindi **vince
  l'ULTIMA regola che combacia**, non la prima. Un diniego scritto prima di un
  permesso non nega niente;
- **che una negazione non venga allargata**: se il seed non concede `Bash`,
  `bash` resta `"deny"` secco e nessuna regola del sandbox lo riapre.

Lo schema è verificato dall'interno del container (`opencode debug config`
rifiuta un'azione fuori da `["ask", "allow", "deny"]`), che è la condizione che
l'issue poneva e che su codex non è soddisfatta.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from . import native_tools as nt
from . import session as S
from .session import OpenCodeChatSession


def _sandbox(**kw) -> nt.NormalizedSandbox:
    return nt.NormalizedSandbox(**{k: tuple(v) for k, v in kw.items()})


def _bash(perm: dict) -> dict:
    """La mappa `bash`, o l'azione secca, come opencode la leggerà."""
    return perm.get("bash")


class TheDeniedPatternsReachOpenCode(unittest.TestCase):

    def test_a_denied_pattern_becomes_a_bash_deny_rule(self):
        perm = nt.opencode_permission(["Bash", "Read"],
                                      _sandbox(deny_shell_patterns=["sudo *"]))
        self.assertEqual(_bash(perm), {"sudo *": "deny"})

    def test_every_declared_pattern_is_carried(self):
        """Come `ophelia`: tre pattern, tre regole. Portarne due sarebbe la
        stessa bugia di non portarne nessuna, meno visibile."""
        perm = nt.opencode_permission(
            ["Bash"], _sandbox(deny_shell_patterns=["rm -rf *", "curl *", "sudo *"]))
        self.assertEqual(_bash(perm),
                         {"rm -rf *": "deny", "curl *": "deny", "sudo *": "deny"})

    def test_the_scratch_placeholder_is_resolved(self):
        """`{scratch}` lasciato letterale sarebbe un divieto che non combacia
        mai — cioè il difetto per cui questo file esiste."""
        sb = nt.normalize_sandbox(
            SimpleNamespace(allow_read=[], deny_read=[], allow_write=[],
                            allow_shell_cmds=[],
                            deny_shell_patterns=["rm -rf {scratch}/*"]),
            "/data/spawns/ophelia-3/scratch")
        perm = nt.opencode_permission(["Bash"], sb)
        self.assertEqual(_bash(perm),
                         {"rm -rf /data/spawns/ophelia-3/scratch/*": "deny"})

    def test_a_seed_silent_on_native_tools_still_gets_its_deny(self):
        """I due elenchi sono assi diversi: `native_tools` dice QUALI strumenti,
        il sandbox come si stringe la shell che il seed ha già. Un seed che non
        si pronuncia sui nativi ha comunque diritto al suo divieto."""
        perm = nt.opencode_permission(None, _sandbox(deny_shell_patterns=["sudo *"]))
        self.assertEqual(perm, {"bash": {"sudo *": "deny"}})

    def test_saying_nothing_emits_nothing(self):
        """Una sezione emessa a vuoto è la porta aperta al difetto opposto."""
        self.assertEqual(nt.opencode_permission(None, _sandbox()), {})
        self.assertEqual(nt.opencode_permission(None, None), {})


class TheOrderIsTheRule(unittest.TestCase):
    """`findLast` = vince l'ultima regola che combacia. Ogni asserzione qui è su
    una POSIZIONE, perché su opencode la posizione È la precedenza."""

    def _keys(self, *args) -> list[str]:
        return list(nt.opencode_permission(*args)["bash"])

    def test_the_catch_all_deny_comes_before_the_commands_it_excepts(self):
        """Il difetto che questo giro corregge: `{"*": "deny"}` era inserito per
        ULTIMO, quindi combaciava per ultimo e vinceva su tutto. Un seed che
        dichiarava `Bash(git:*)` si ritrovava la shell chiusa anche a `git` — la
        restrizione giusta portata al runtime nel modo che la rende totale."""
        chiavi = self._keys(["Bash(git:*)"], None)
        self.assertEqual(chiavi[0], "*")
        self.assertLess(chiavi.index("*"), chiavi.index("git"))
        self.assertLess(chiavi.index("*"), chiavi.index("git *"))

    def test_a_denied_pattern_wins_over_a_granted_command(self):
        """Su claude `permissions.deny` ha precedenza su `allow`. Qui la stessa
        precedenza si ottiene con l'ordine: i dinieghi del sandbox in coda."""
        chiavi = self._keys(["Bash(git:*)"], _sandbox(deny_shell_patterns=["git push *"]))
        self.assertLess(chiavi.index("git *"), chiavi.index("git push *"))
        perm = nt.opencode_permission(["Bash(git:*)"],
                                      _sandbox(deny_shell_patterns=["git push *"]))
        self.assertEqual(perm["bash"]["git push *"], "deny")
        self.assertEqual(perm["bash"]["git *"], "allow")


class ANegationIsNeverWidened(unittest.TestCase):

    def test_without_bash_the_shell_stays_denied_whole(self):
        """`bash: "deny"` secco è più forte di qualunque mappa: se il seed non
        concede `Bash`, il sandbox non ha una shell da stringere e non deve
        scriverne una da riaprire."""
        perm = nt.opencode_permission(["Read"],
                                      _sandbox(deny_shell_patterns=["sudo *"]))
        self.assertEqual(perm["bash"], "deny")

    def test_the_other_keys_are_untouched_by_the_sandbox(self):
        perm = nt.opencode_permission(["Bash", "Read"],
                                      _sandbox(deny_shell_patterns=["sudo *"]))
        self.assertEqual(perm["websearch"], "deny")
        self.assertNotIn("read", perm)


class TheAllowListStaysOut(unittest.TestCase):
    """`allow_shell_cmds` NON viene tradotto, e la tabella lo dice.

    Misurato: la lista di regole di un agente opencode comincia con
    `{permission: "*", pattern: "*", action: "allow"}`, quindi un comando che
    nessuna regola nomina è **concesso**. Un elenco di permessi non può
    restringere niente là dentro; per farlo diventare una porta servirebbe un
    `{"*": "deny"}` che il seed non ha chiesto e che toglierebbe la shell ai tre
    seed non-claude che oggi la usano. È una decisione, non una traduzione.
    """

    def test_the_allowed_commands_are_not_emitted(self):
        perm = nt.opencode_permission(["Bash"],
                                      _sandbox(allow_shell_cmds=["git", "pytest"]))
        self.assertNotIn("bash", perm)

    def test_the_table_still_calls_the_allow_list_unenforced(self):
        from ..agents.models import Sandbox
        self.assertEqual(
            nt.sandbox_unenforced("opencode", Sandbox(allow_shell_cmds=["git"])),
            ["allow_shell_cmds"])

    def test_the_table_no_longer_calls_the_deny_list_unenforced(self):
        from ..agents.models import Sandbox
        self.assertEqual(
            nt.sandbox_unenforced("opencode", Sandbox(deny_shell_patterns=["sudo *"])),
            [])

    def test_the_paths_stay_out_too(self):
        """`read`/`edit` di opencode ricevono il path **relativo al worktree**
        (misurato: `patterns: [relative(worktree, file)]`), mentre i campi del
        seed sono assoluti col `{scratch}` risolto: un `/data/spawns/x/**` non
        combacerebbe mai. Tradurli è un'altra decisione, e finché non è presa
        la tabella deve continuare a dirli inerti."""
        from ..agents.models import Sandbox
        self.assertEqual(
            nt.sandbox_unenforced("opencode", Sandbox(allow_write=["{scratch}/**"],
                                                      allow_read=["/x/**"],
                                                      deny_read=["/y/**"])),
            ["allow_read", "deny_read", "allow_write"])


class ItArrivesInTheConfigFile(unittest.TestCase):
    """Il pezzo che chiude il giro: la traduzione esiste E qualcuno la scrive.
    Senza questo test la funzione potrebbe essere giusta e non chiamata da
    nessuno — che è esattamente il difetto dell'issue."""

    def _config(self, native, sandbox_kw: dict) -> dict:
        from ..agents.models import Sandbox
        spec = SimpleNamespace(reasoning_effort=None, sandbox=Sandbox(**sandbox_kw))
        sess = OpenCodeChatSession.__new__(OpenCodeChatSession)
        sess.kind = "impiegato-tomato"
        sess.chat_id = "chan:SEAL-2:test:impiegato-tomato"
        sess.principal = "davide"
        sess._runtime_override = {}
        sess._provider = None
        sess._model = None
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(S, "_runtime_provider", return_value="scaleway"), \
             mock.patch.object(S, "_runtime_model", return_value="gpt-oss-120b"), \
             mock.patch.object(S, "_kind_spec", return_value=spec), \
             mock.patch.object(S, "_resolve_native_allowed", return_value=native), \
             mock.patch("server.api.providers._read", return_value={}), \
             mock.patch("server.api.providers.provider_extra_env", return_value={}), \
             mock.patch.object(S.pki, "mint_session_token", return_value="ckt1.test"):
            sess._write_config(Path(td))
            return json.loads((Path(td) / "opencode.json").read_text(encoding="utf-8"))

    def test_the_seed_sandbox_reaches_opencode_json(self):
        cfg = self._config(["Bash", "Read"], {"deny_shell_patterns": ["sudo *"]})
        self.assertEqual(cfg["permission"]["bash"], {"sudo *": "deny"})

    def test_the_scratch_of_this_spawn_is_the_one_resolved(self):
        cfg = self._config(["Bash"], {"deny_shell_patterns": ["rm -rf {scratch}/*"]})
        chiave = next(iter(cfg["permission"]["bash"]))
        self.assertTrue(chiave.endswith("/scratch/*"), chiave)
        self.assertNotIn("{scratch}", chiave)

    def test_a_seed_without_sandbox_gets_no_bash_map(self):
        cfg = self._config(["Bash", "Read"], {})
        self.assertNotIn("bash", cfg.get("permission", {}))


if __name__ == "__main__":
    unittest.main()
