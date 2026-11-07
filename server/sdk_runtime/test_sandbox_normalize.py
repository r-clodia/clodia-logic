"""La lettura dei campi `sandbox` del seed sta in un punto solo.

Finora la pulizia di quegli elenchi viveva dentro `agents/workspace`, cioè
dentro il traduttore di claude, perché claude era l'unico che li portava
(clodia-platform#296). Con la traduzione su opencode in arrivo (punto 2 della
stessa issue) quel codice avrebbe avuto un gemello, e due letture separate degli
stessi campi divergono alla prima correzione — sul lato che nessuno rilegge.

`native_tools.normalize_sandbox` è quel punto solo: risolve `{scratch}`, toglie
spazi e voci vuote, scarta i doppioni tenendo l'ordine. Non decide nulla —
se un `allow_shell_cmds` dichiarato debba valere come allowlist con diniego
implicito è una decisione che vive nel traduttore, un piano sopra. Since #656
the fields are only the two shell keys.

Cosa si misura:
- il default del campo assente è esplicito e non restringe;
- il segnaposto è risolto in TUTTI i campi, anche nei pattern di shell, dove
  lasciarlo letterale scriveva un divieto che non combacia mai;
- gli elenchi ripuliti (spazi, vuoti, doppioni, ordine di dichiarazione);
- claude passa DAVVERO di lì: il `.claude/settings.json` eredita la pulizia
  invece di rifarsela in casa.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from ..agents import workspace as ws
from ..agents.models import Sandbox
from . import native_tools as nt

SCRATCH = Path("/clodia/spawns/ophelia-7/scratch")


class NormalizeSandboxTests(unittest.TestCase):

    def test_no_sandbox_is_the_explicit_empty_default(self):
        """Seed senza sezione `sandbox`: tutti i campi vuoti, nessuno dichiarato.
        Chi tace non restringe — la direzione d'errore di tutto il file."""
        n = nt.normalize_sandbox(None)
        for campo in nt.SANDBOX_FIELDS:
            self.assertEqual((), getattr(n, campo), campo)
        self.assertEqual((), n.declared())

    def test_missing_field_defaults_to_empty_and_stays_undeclared(self):
        """Un campo non dichiarato non diventa un elenco inventato: resta vuoto,
        e `declared()` nomina solo quelli su cui il seed si è pronunciato."""
        n = nt.normalize_sandbox(Sandbox(deny_shell_patterns=["rm -rf {scratch}/*"]),
                                 SCRATCH)
        self.assertEqual((), n.allow_shell_cmds)
        self.assertEqual(("deny_shell_patterns",), n.declared())

    def test_scratch_placeholder_resolved_in_every_field(self):
        """Anche nei campi di shell: `rm -rf {scratch}/*` lasciato letterale è
        una regola che non combacia mai, cioè un divieto scritto e mai applicato."""
        n = nt.normalize_sandbox(Sandbox(
            allow_shell_cmds=["{scratch}/bin/tool"],
            deny_shell_patterns=["rm -rf {scratch}/*"],
        ), SCRATCH)
        self.assertEqual((f"{SCRATCH}/bin/tool",), n.allow_shell_cmds)
        self.assertEqual((f"rm -rf {SCRATCH}/*",), n.deny_shell_patterns)
        self.assertEqual(list(nt.SANDBOX_FIELDS), list(n.declared()))

    def test_without_scratch_the_placeholder_stays_literal(self):
        """Chi la forma la deve solo MOSTRARE (scheda, avvisi del loader) non ha
        uno spawn: stampare un path inventato mentirebbe."""
        n = nt.normalize_sandbox(Sandbox(deny_shell_patterns=["rm -rf {scratch}/*"]))
        self.assertEqual(("rm -rf {scratch}/*",), n.deny_shell_patterns)

    def test_entries_are_stripped_deduped_and_ordered(self):
        """Spazi via, voci vuote via, doppioni una volta sola, ordine di
        dichiarazione intatto."""
        n = nt.normalize_sandbox(Sandbox(
            allow_shell_cmds=["  git ", "pytest", "git", "", "   ", "ls"],
        ))
        self.assertEqual(("git", "pytest", "ls"), n.allow_shell_cmds)

    def test_dedup_happens_after_the_placeholder_is_resolved(self):
        """Due voci diverse nel seed possono essere lo stesso path una volta
        risolto il segnaposto: il doppione si vede solo dopo."""
        n = nt.normalize_sandbox(Sandbox(
            deny_shell_patterns=["rm -rf {scratch}/*", f"rm -rf {SCRATCH}/*"],
        ), SCRATCH)
        self.assertEqual((f"rm -rf {SCRATCH}/*",), n.deny_shell_patterns)

    def test_only_the_shell_keys_are_fields(self):
        """#656: the path keys are gone from the normalized form too."""
        self.assertEqual(("allow_shell_cmds", "deny_shell_patterns"),
                         nt.SANDBOX_FIELDS)
        n = nt.normalize_sandbox(None)
        for gone in ("allow_read", "deny_read", "allow_write"):
            self.assertFalse(hasattr(n, gone), gone)


class ClaudeUsesTheSameNormalizationTests(unittest.TestCase):
    """Il traduttore di claude consuma la forma normalizzata, non il seed grezzo:
    è la prova che il punto solo è davvero uno."""

    def _settings(self, sandbox: Sandbox) -> dict:
        spec = type("SpecFinta", (), {"sandbox": sandbox})()
        return ws._build_settings_json(spec, SCRATCH)["permissions"]

    def test_claude_translation_inherits_dedup_and_strip(self):
        perms = self._settings(Sandbox(
            allow_shell_cmds=["  git ", "git", ""],
        ))
        self.assertEqual(["Bash(git *)"], perms["allow"])

    def test_claude_deny_patterns_get_the_scratch_resolved(self):
        perms = self._settings(Sandbox(deny_shell_patterns=["rm -rf {scratch}/*"]))
        self.assertEqual([f"Bash(rm -rf {SCRATCH}/*)"], perms["deny"])

    def test_a_legacy_seed_emits_no_read_or_write_rule(self):
        """#656: a seed that still declares the path keys (older seeds, external
        packs) loads, and the workspace emits only the shell rules — no
        `Read(...)`, `Write(...)` or `Edit(...)` from the sandbox."""
        perms = self._settings(Sandbox.model_validate({
            "allow_read": ["{scratch}/**"],
            "deny_read": ["/clodia/secrets/**", "/datadir/secrets/**"],
            "allow_write": ["{scratch}/**"],
            "allow_shell_cmds": ["git", "pytest"],
            "deny_shell_patterns": ["rm -rf *", "curl *"],
        }))
        self.assertEqual(["Bash(git *)", "Bash(pytest *)"], perms["allow"])
        self.assertEqual(["Bash(rm -rf *)", "Bash(curl *)"], perms["deny"])
        for rule in perms["allow"] + perms["deny"]:
            self.assertFalse(rule.startswith(("Read(", "Write(", "Edit(")), rule)


if __name__ == "__main__":
    unittest.main()
