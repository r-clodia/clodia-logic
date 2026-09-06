"""Un recapito sbagliato non deve poter rompere l'agente su cui è scritto.

`PATCH /api/agents/{name}` scrive `agent.yaml` e POI ricarica la registry.
Finché il campo `telegram` non aveva una forma, qualunque stringa veniva scritta
e il difetto restava latente; ora che lo schema la valida, la stessa PATCH
scriverebbe sul disco un valore che il loader rifiuta — l'agente sparirebbe
dalla registry e l'endpoint uscirebbe con un 500 «dopo la modifica l'agent non
valida», lasciando una persona senza scheda per un carattere di troppo.

La direzione giusta è rifiutare PRIMA di scrivere: 400, il file com'era, e un
messaggio che dice quale forma recapita (clodia-platform#200).
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi import HTTPException

from . import agent_registry as AR
from ..agents.loader import AgentRegistry

SEED = {"name": "davide", "display_name": "Davide", "description": "Principal umano",
        "type": "human", "role": "superadmin", "clearance": "SEAL-4"}


class PatchTelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "davide"
        self.dir.mkdir()
        self.yaml_path = self.dir / "agent.yaml"
        self.yaml_path.write_text(yaml.safe_dump(SEED, sort_keys=False))
        reg = AgentRegistry(base_dir=Path(self.tmp.name))
        reg.load()
        for p in (patch.object(AR, "registry", reg),
                  patch.object(AR.admin, "is_admin", lambda p: True),
                  patch.object(AR, "_principal_from_request", lambda r: "davide")):
            p.start()
            self.addCleanup(p.stop)

    def _patch(self, **fields):
        return asyncio.run(AR.patch_agent("davide", AR.AgentPatch(**fields), None))

    def test_a_link_is_refused_before_anything_is_written(self):
        prima = self.yaml_path.read_text()
        with self.assertRaises(HTTPException) as e:
            self._patch(telegram="https://t.me/davide_c")
        self.assertEqual(400, e.exception.status_code)
        self.assertIn("chat_id", str(e.exception.detail))
        self.assertEqual(prima, self.yaml_path.read_text(),
                         "il seed è stato toccato da una PATCH rifiutata")

    def test_a_valid_id_lands_in_the_seed(self):
        spec = self._patch(telegram="76632169")
        self.assertEqual("76632169", spec.telegram)
        self.assertEqual("76632169", yaml.safe_load(self.yaml_path.read_text())["telegram"])

    def test_a_handle_is_stored_canonical(self):
        """Chi scrive `davide_c` e chi scrive `@davide_c` deve ritrovare lo
        stesso file: la forma canonica si decide una volta, in scrittura."""
        self.assertEqual("@davide_c", self._patch(telegram="davide_c").telegram)
        self.assertEqual("@davide_c", yaml.safe_load(self.yaml_path.read_text())["telegram"])

    def test_the_empty_string_still_clears_the_field(self):
        self._patch(telegram="76632169")
        self.assertIsNone(self._patch(telegram="").telegram)
        self.assertNotIn("telegram", yaml.safe_load(self.yaml_path.read_text()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
