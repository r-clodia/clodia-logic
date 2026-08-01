"""Test partecipazione multi-spawn (issue clodia-platform#94).

Copre lato unit la DoD: parsing tag con ordinale, risoluzione dell'istanza
(minimo libero / fork / cap / esplicito), self-skip della delega per seed,
snapshot memory read-only per gli ordinali >1.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from . import channels as ch


def _spec(name="fullstack-dev", multi=True, cap=4):
    return SimpleNamespace(name=name, multi_spawn=multi, max_spawns=cap)


def _chat(chat_id, busy):
    lock = SimpleNamespace(locked=lambda: busy)
    return SimpleNamespace(chat_id=chat_id, _lock=lock)


def _with_sessions(sessions):
    return patch.object(ch.manager, "list", return_value=sessions)


class TagOrdinalTests(unittest.TestCase):
    def test_tag_with_ordinal_captured_whole(self) -> None:
        hard, soft = ch._tags("fai tu @fullstack-dev#2 e $anna")
        self.assertEqual(hard, ["fullstack-dev#2"])
        self.assertEqual(soft, ["anna"])

    def test_split_ord(self) -> None:
        self.assertEqual(ch._split_ord("fullstack-dev#2"), ("fullstack-dev", 2))
        self.assertEqual(ch._split_ord("fullstack-dev"), ("fullstack-dev", None))
        self.assertEqual(ch._split_ord(None), (None, None))

    def test_seed_name(self) -> None:
        self.assertEqual(ch._seed_name("fullstack-dev#12"), "fullstack-dev")
        self.assertEqual(ch._seed_name("clodia"), "clodia")


class ResolveOrdinalTests(unittest.TestCase):
    """DoD 2-4: minimo libero, fork se tutti occupati, cap, esplicito."""

    PREFIX = "chan:SEAL-1:ch:fullstack-dev#"

    def test_no_instances_starts_at_1(self) -> None:
        with _with_sessions([]):
            self.assertEqual(ch._resolve_ordinal("SEAL-1", "ch", _spec(), None), 1)

    def test_lowest_free_wins(self) -> None:
        sessions = [_chat(self.PREFIX + "1", busy=True),
                    _chat(self.PREFIX + "2", busy=False),
                    _chat(self.PREFIX + "3", busy=False)]
        with _with_sessions(sessions):
            self.assertEqual(ch._resolve_ordinal("SEAL-1", "ch", _spec(), None), 2)

    def test_all_busy_forks_next(self) -> None:
        sessions = [_chat(self.PREFIX + "1", busy=True),
                    _chat(self.PREFIX + "2", busy=True)]
        with _with_sessions(sessions):
            self.assertEqual(ch._resolve_ordinal("SEAL-1", "ch", _spec(), None), 3)

    def test_cap_reached_queues_on_lowest(self) -> None:
        sessions = [_chat(self.PREFIX + "1", busy=True),
                    _chat(self.PREFIX + "2", busy=True)]
        with _with_sessions(sessions):
            self.assertEqual(ch._resolve_ordinal("SEAL-1", "ch", _spec(cap=2), None), 1)

    def test_explicit_ordinal_respected_even_if_others_free(self) -> None:
        sessions = [_chat(self.PREFIX + "1", busy=False)]
        with _with_sessions(sessions):
            self.assertEqual(ch._resolve_ordinal("SEAL-1", "ch", _spec(), 2), 2)

    def test_explicit_ordinal_clamped_to_cap(self) -> None:
        with _with_sessions([]):
            self.assertEqual(ch._resolve_ordinal("SEAL-1", "ch", _spec(cap=2), 7), 2)

    def test_other_channels_do_not_interfere(self) -> None:
        sessions = [_chat("chan:SEAL-1:altro:fullstack-dev#1", busy=True)]
        with _with_sessions(sessions):
            self.assertEqual(ch._resolve_ordinal("SEAL-1", "ch", _spec(), None), 1)


class SeedComparisonTests(unittest.TestCase):
    """DoD 6: i confronti self/dedup e la soppressione doppia risposta
    lavorano sul seed, non sulla label istanza."""

    def test_new_ai_messages_matches_seed(self) -> None:
        before: list[dict] = []
        after = [{"author": "fullstack-dev", "kind": "ai", "text": "posted via tool",
                  "id": "1", "ts": "t"}]
        got = ch._new_ai_messages(before, after, "fullstack-dev#2")
        self.assertEqual(len(got), 1)

    def test_new_ai_messages_other_seed_excluded(self) -> None:
        after = [{"author": "clodia", "kind": "ai", "text": "x", "id": "1", "ts": "t"}]
        self.assertEqual(ch._new_ai_messages([], after, "fullstack-dev#2"), [])


class MemoryReadonlyTests(unittest.TestCase):
    """DoD 7: ordinale >1 → memory snapshot in sola lettura; cleanup ok."""

    def _make_ws(self, readonly: bool):
        from ..agents.models import AgentSpec
        from ..agents.workspace import EphemeralWorkspace
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        agent_dir = base / "agents" / "dev"
        (agent_dir / "memory").mkdir(parents=True)
        (agent_dir / "memory" / "MEMORY.md").write_text("ricordi", encoding="utf-8")
        (agent_dir / "prompt.md").write_text("sp", encoding="utf-8")
        spec = AgentSpec.model_validate({
            "name": "dev", "description": "d", "display_name": "Dev",
            "model": "m", "system_prompt": "prompt.md",
            "memory": {"dir": "memory/"}, "agent_dir": str(agent_dir),
        })
        import server.agents.workspace as wmod
        patcher = patch.object(wmod, "WORKSPACES_ROOT", base / "spawns")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = patch.object(wmod, "SPAWNS_ROOT", base / "spawns")
        patcher2.start()
        self.addCleanup(patcher2.stop)
        ws = EphemeralWorkspace(spec, task_id="t1", memory_readonly=readonly)
        return ws, agent_dir

    def test_readonly_snapshot_not_writable_and_source_untouched(self) -> None:
        ws, agent_dir = self._make_ws(readonly=True)
        d = ws.create()
        mem = d / ".agent" / "memory"
        self.assertFalse(mem.is_symlink())
        self.assertEqual((mem / "MEMORY.md").read_text(encoding="utf-8"), "ricordi")
        with self.assertRaises(PermissionError):
            (mem / "MEMORY.md").open("a")
        with self.assertRaises(PermissionError):
            (mem / "nuovo.md").write_text("x", encoding="utf-8")
        ws.cleanup()
        self.assertFalse(d.exists())
        # la memory del SEED è intatta (il create appende solo il blocco
        # feedback-lessons standard) e resta scrivibile
        src = agent_dir / "memory" / "MEMORY.md"
        self.assertTrue(src.read_text(encoding="utf-8").startswith("ricordi"))
        src.write_text("aggiornata", encoding="utf-8")

    def test_default_symlink_rw(self) -> None:
        ws, agent_dir = self._make_ws(readonly=False)
        d = ws.create()
        mem = d / ".agent" / "memory"
        self.assertTrue(mem.is_symlink())
        (mem / "MEMORY.md").write_text("scrivo", encoding="utf-8")
        self.assertEqual((agent_dir / "memory" / "MEMORY.md").read_text(encoding="utf-8"),
                         "scrivo")
        ws.cleanup()
        self.assertTrue((agent_dir / "memory" / "MEMORY.md").is_file())


if __name__ == "__main__":
    unittest.main()
