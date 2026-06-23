"""Test del job-agent dinamico (19 giu 2026).

Copre:
  - schema job con campo `agent` (create default clodia, back-compat read, update);
  - risoluzione dinamica dei kind in sdk_runtime.session via registry seed.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from . import db
from ..agents.loader import registry
from ..agents.models import AgentSpec
from ..sdk_runtime import session as s


class JobAgentFieldTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = db.JOBS_DIR
        db.JOBS_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        db.JOBS_DIR = self._old_dir
        self._tmp.cleanup()

    def test_create_defaults_to_clodia(self) -> None:
        job = db.create_job("j1", "*/5 * * * *", "ciao")
        self.assertEqual(job["agent"], "clodia")
        self.assertEqual(db.get_job(job["id"])["agent"], "clodia")

    def test_create_with_explicit_agent(self) -> None:
        job = db.create_job("j2", "*/5 * * * *", "ciao", agent="ophelia")
        self.assertEqual(db.get_job(job["id"])["agent"], "ophelia")

    def test_legacy_job_without_agent_reads_as_looper(self) -> None:
        # Simula un job scritto prima dell'introduzione del campo `agent`.
        (db.JOBS_DIR / "7.yaml").write_text(yaml.safe_dump({
            "id": 7, "name": "vecchio", "cron_expr": "0 9 * * *",
            "prompt": "x", "enabled": True,
        }), encoding="utf-8")
        self.assertEqual(db.get_job(7)["agent"], "looper")

    def test_update_agent(self) -> None:
        job = db.create_job("j3", "*/5 * * * *", "ciao")
        db.update_job(job["id"], agent="ada")
        self.assertEqual(db.get_job(job["id"])["agent"], "ada")


class DynamicKindResolutionTests(unittest.TestCase):
    """Inietta un agent fittizio nel registry e verifica la risoluzione."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        agent_dir = Path(self._tmp.name) / "webdev"
        agent_dir.mkdir()
        self._spec = AgentSpec.model_validate({
            "name": "webdev",
            "description": "web dev",
            "model": "claude-sonnet-4-6",
            "display_name": "WebDev",
            "agent_sdk": "claude",
            "system_prompt": "system-prompt.md",
        })
        self._spec.agent_dir = str(agent_dir)
        self._codex_spec = AgentSpec.model_validate({
            "name": "saimx",
            "description": "codex dev",
            "model": "gpt-5.5",
            "display_name": "SaimX",
            "agent_sdk": "codex",
            "system_prompt": "system-prompt.md",
        })
        self._saved = dict(registry._agents)
        registry._agents["webdev"] = self._spec
        registry._agents["saimx"] = self._codex_spec

    def tearDown(self) -> None:
        registry._agents = self._saved
        self._tmp.cleanup()

    def test_known_and_available(self) -> None:
        self.assertTrue(s.known_kind("webdev"))
        self.assertTrue(s.known_kind("clodia"))     # statico
        self.assertFalse(s.known_kind("inesistente"))
        self.assertIn("webdev", s.available_kinds())
        self.assertIn("clodia", s.available_kinds())

    def test_dynamic_model_from_seed(self) -> None:
        self.assertEqual(s._resolve_model("webdev"), "claude-sonnet-4-6")
        # i kind statici restano invariati (clodia = default del CLI → None)
        self.assertIsNone(s._resolve_model("clodia"))

    def test_dynamic_cwd_from_agent_dir(self) -> None:
        self.assertEqual(str(s._resolve_cwd("webdev")), self._spec.agent_dir)

    def test_dynamic_permission_and_no_blocklist(self) -> None:
        self.assertEqual(s._resolve_permission_mode("webdev"), "bypassPermissions")
        self.assertEqual(s._resolve_disallowed_tools("webdev"), [])

    def test_runtime_from_agent_sdk(self) -> None:
        self.assertFalse(s._is_codex_kind("webdev"))   # claude
        self.assertTrue(s._is_codex_kind("saimx"))     # codex
        self.assertTrue(s._is_codex_kind("ophelia"))   # statico codex

    def test_session_construct_dynamic_kind(self) -> None:
        # La guardia non deve sollevare per un kind del registry.
        chat = s.ChatSession("c-test", kind="webdev")
        self.assertEqual(chat.kind, "webdev")
        self.assertTrue(chat.title.startswith("[WEBD]"))


class ProviderEnforcementTests(unittest.TestCase):
    """Un agent col provider scollegato non è disponibile (chat/job)."""

    def setUp(self) -> None:
        from ..agents.models import AgentSpec
        self._saved = dict(registry._agents)
        registry._agents["claudette"] = AgentSpec.model_validate({
            "name": "claudette", "description": "d", "model": "m",
            "display_name": "C", "agent_sdk": "claude", "system_prompt": "s.md"})
        import server.api.providers as P
        self._P = P
        self._orig = P.connected_provider_ids

    def tearDown(self) -> None:
        registry._agents = self._saved
        self._P.connected_provider_ids = self._orig

    def test_agent_provider_resolved(self) -> None:
        # claude→[anthropic-api, claude-pro-max]; nessuno collegato → preferito.
        self.assertEqual(s.agent_provider("claudette"), "anthropic-api")

    def test_connected_passes(self) -> None:
        self._P.connected_provider_ids = lambda: {"anthropic-api"}
        self.assertTrue(s.provider_connected_for("claudette"))
        s._ensure_provider_connected("claudette")  # non solleva

    def test_disconnected_blocks(self) -> None:
        self._P.connected_provider_ids = lambda: set()
        self.assertFalse(s.provider_connected_for("claudette"))
        with self.assertRaises(s.ProviderNotConnected):
            s._ensure_provider_connected("claudette")

    def test_unknown_provider_not_blocked(self) -> None:
        # opencode → provider non derivabile → non bloccato (fail-open).
        from ..agents.models import AgentSpec
        registry._agents["oc"] = AgentSpec.model_validate({
            "name": "oc", "description": "d", "model": "m",
            "display_name": "OC", "agent_sdk": "opencode", "system_prompt": "s.md"})
        self._P.connected_provider_ids = lambda: set()
        self.assertTrue(s.provider_connected_for("oc"))
        s._ensure_provider_connected("oc")  # non solleva


if __name__ == "__main__":
    unittest.main()
