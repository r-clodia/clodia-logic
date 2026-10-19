"""#531 — a retired platform agent (`messaggero`) keeps its name reserved.

An existing instance may still hold its seed, PKI identity, vault grants and
gateway entry. If a pack could install or own an agent with that name, it
would inherit all of them (the class of #564). The residue is archived at boot.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from . import pack_import, packs


class RetiredNameTests(unittest.TestCase):
    def test_the_name_is_reserved_for_packs(self) -> None:
        self.assertIn("messaggero", pack_import.RETIRED_AGENTS)
        self.assertIn("messaggero", pack_import.RESERVED_AGENT_NAMES)

    def test_a_pack_cannot_install_it_even_with_force(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sdir = Path(td) / "messaggero"
            sdir.mkdir()
            (sdir / "agent.yaml").write_text("name: messaggero\n", encoding="utf-8")
            for force in (False, True):
                with self.subTest(force=force):
                    out = pack_import._install_seed(sdir, force=force)
                    self.assertEqual(out["status"], "error", out)

    def test_an_update_never_owns_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "acme").mkdir()
            (root / "acme" / "pack.yaml").write_text(
                "name: acme\nagents: [acme-agent, messaggero]\n", encoding="utf-8")
            with patch.object(pack_import, "PACKS_META_DIR", root):
                self.assertEqual(packs._owned_seeds("acme"), {"acme-agent"})


class RetireAtBootTests(unittest.TestCase):
    def test_the_residue_is_archived_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "agents"
            (base / "messaggero" / "memory").mkdir(parents=True)
            (base / "messaggero" / "memory" / "MEMORY.md").write_text("x", encoding="utf-8")
            (base / "clodia").mkdir()
            archive = Path(td) / "agents-archive"
            fake = type("R", (), {"base_dir": base, "load": lambda self: None})()
            with patch.object(pack_import, "registry", fake), \
                    patch.object(pack_import, "_agents_archive_root", lambda: archive):
                out = pack_import.retire_agents()
            self.assertEqual([r["agent"] for r in out], ["messaggero"])
            self.assertFalse((base / "messaggero").exists())
            self.assertTrue((base / "clodia").exists())
            self.assertTrue(any(archive.rglob("MEMORY.md")))


class CreateAgentTests(unittest.TestCase):
    def test_the_api_refuses_the_retired_name(self) -> None:
        import asyncio
        from . import admin, agent_registry
        body = agent_registry.AgentCreate.model_validate(
            {"name": "messaggero", "type": "proxy"})
        with patch.object(admin, "is_initialized", lambda: False):
            with self.assertRaises(HTTPException) as cm:
                asyncio.run(agent_registry.create_agent(body, None))
        self.assertEqual(cm.exception.status_code, 409)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
