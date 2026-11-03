"""#651 — an immutable agent (clodia) may acquire skills (owner, 3 Nov 2026).

`PATCH /api/agents/<n>/caps` refused EVERY change to an immutable seed, so
clodia could never be given a new skill. Its capabilities now change like any
agent's (catalog-validated); its rules stay code/rebuild only.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml
from fastapi import HTTPException

from . import agent_registry as AR

SEED = """name: clodia
type: normal
immutable: true
capabilities:
- base-pack/topic-management
rules: []
"""


class _Registry:
    def __init__(self, adir: Path):
        self.adir = adir
        self.load()

    def load(self):
        d = yaml.safe_load((self.adir / "agent.yaml").read_text()) or {}
        self._spec = SimpleNamespace(name=d["name"], agent_dir=str(self.adir),
                                     immutable=bool(d.get("immutable")),
                                     capabilities=list(d.get("capabilities") or []),
                                     rules=list(d.get("rules") or []), tool_permissions=[])

    def get_by_name(self, name):
        return self._spec if name == self._spec.name else None

    def errors(self):
        return {}


class ImmutableAcquiresSkillsTests(unittest.TestCase):
    def _patch(self, **fields):
        with tempfile.TemporaryDirectory() as tmp:
            adir = Path(tmp) / "clodia"
            adir.mkdir()
            (adir / "agent.yaml").write_text(SEED, encoding="utf-8")
            reg = _Registry(adir)
            with patch.object(AR, "registry", reg), \
                    patch.object(AR, "_principal_from_request", lambda r: "sysadmin"), \
                    patch.object(AR, "_agent_can_admin", lambda c: True), \
                    patch.object(AR, "_validate_catalog_refs", lambda refs, kind: None):
                try:
                    out, err = asyncio.run(AR.patch_agent_caps(
                        "clodia", AR.AgentCapsPatch(**fields), None)), None
                except HTTPException as e:
                    out, err = None, e
            return out, err, yaml.safe_load((adir / "agent.yaml").read_text())

    def test_skills_are_added_to_an_immutable_seed(self):
        out, err, disk = self._patch(capabilities=["base-pack/topic-management",
                                                   "business-pack/stima-pert"])
        self.assertIsNone(err)
        self.assertIn("business-pack/stima-pert", disk["capabilities"])
        self.assertTrue(disk["immutable"])

    def test_rules_stay_locked(self):
        out, err, disk = self._patch(rules=["x/regola"])
        self.assertEqual(err.status_code, 403)
        self.assertEqual(disk["rules"], [])

    def test_skills_and_rules_together_change_nothing(self):
        out, err, disk = self._patch(capabilities=["business-pack/stima-pert"],
                                     rules=["x/regola"])
        self.assertEqual(err.status_code, 403)
        self.assertEqual(disk["capabilities"], ["base-pack/topic-management"])


if __name__ == "__main__":
    unittest.main()
