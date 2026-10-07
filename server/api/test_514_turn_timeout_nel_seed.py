"""Il budget del turno si imposta sull'AGENTE, e si scrive nel suo seed
(clodia-platform#514).

Finché l'unica leva è stata la variabile d'ambiente `OPENCODE_TURN_TIMEOUT`, chi
amministra l'istanza poteva solo spostare la soglia per TUTTI gli agenti
insieme: dare più tempo all'esecutore di tool che non converge significava
toglierlo di mezzo anche a chi, sullo stesso processo, deve fallire in fretta.

Qui si verifica la via applicativa: `PATCH /api/agents/{name}` scrive
`turn_timeout` in `agent.yaml` come INTERO (non come stringa: riletto dallo
YAML deve restare un numero), `0` rimette il default togliendo il campo, e un
valore che lo schema rifiuterebbe viene respinto PRIMA di scrivere — per la
ragione già fissata dalla #200: l'endpoint scrive e poi ricarica, quindi un
valore invalido lascerebbe sul disco un seed che non carica più.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi import HTTPException
from pydantic import ValidationError

from . import agent_registry as AR
from ..agents.loader import AgentRegistry
from ..agents.models import AgentSpec

SEED = {"name": "officer", "display_name": "Officer", "description": "Esecutore",
        "model": "glm-5.2", "agent_sdk": "opencode", "clearance": "SEAL-1"}


class TurnTimeoutNelSeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "officer"
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
        return asyncio.run(AR.patch_agent("officer", AR.AgentPatch(**fields), None))

    def _yaml(self) -> dict:
        return yaml.safe_load(self.yaml_path.read_text())

    def test_il_default_e_nessun_budget_dichiarato(self) -> None:
        """Il seed che tace resta sul default di piattaforma: la #514 aggiunge
        una leva, non cambia il comportamento di chi non la usa."""
        self.assertIsNone(AR.registry.get_by_name("officer").turn_timeout)

    def test_il_budget_finisce_nel_seed_come_numero(self) -> None:
        """IL DIFETTO, in forma di test: oggi il campo non esiste né nello
        schema né nella PATCH, e il budget non è impostabile per agente."""
        spec = self._patch(turn_timeout=600)
        self.assertEqual(600, spec.turn_timeout)
        self.assertEqual(600, self._yaml()["turn_timeout"],
                         "scritto come stringa: rileggerlo darebbe un tipo diverso")

    def test_lo_zero_rimette_il_default(self) -> None:
        """Alzare una soglia senza poterla riabbassare è una leva a senso unico;
        e uno zero SCRITTO nel seed fermerebbe ogni turno dell'agente."""
        self._patch(turn_timeout=600)
        self.assertIsNone(self._patch(turn_timeout=0).turn_timeout)
        self.assertNotIn("turn_timeout", self._yaml())

    def test_un_valore_negativo_e_respinto_prima_di_scrivere(self) -> None:
        prima = self.yaml_path.read_text()
        with self.assertRaises(HTTPException) as e:
            self._patch(turn_timeout=-5)
        self.assertEqual(400, e.exception.status_code)
        self.assertIn("turn_timeout", str(e.exception.detail))
        self.assertEqual(prima, self.yaml_path.read_text(),
                         "il seed è stato toccato da una PATCH rifiutata")

    def test_una_patch_che_non_lo_nomina_non_lo_tocca(self) -> None:
        """Il campo non dichiarato nella PATCH resta com'era: le altre modifiche
        non devono poter azzerare un budget impostato."""
        self._patch(turn_timeout=600)
        self.assertEqual(600, self._patch(description="Esecutore di tool").turn_timeout)
        self.assertEqual(600, self._yaml()["turn_timeout"])

    def test_lo_schema_rifiuta_un_budget_nullo_o_negativo(self) -> None:
        """La guardia sul seed scritto a mano: un `turn_timeout: 0` in
        `agent.yaml` non deve caricare, o l'agente non risponderebbe mai e il
        guasto si leggerebbe come un timeout del provider."""
        for valore in (0, -1):
            with self.assertRaises(ValidationError, msg=f"turn_timeout={valore}"):
                AgentSpec(**SEED, turn_timeout=valore)
        self.assertEqual(300, AgentSpec(**SEED, turn_timeout=300).turn_timeout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
