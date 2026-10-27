"""Chi può ALLARGARE la composizione di un canale, fra i seed del base-pack.

Dal 27 ott 2026 (clodia-platform#626) i seed non dichiarano verbi: chi può
invitare (`topic.add_participant`, gated) e chi può fabbricare autorità
(`agents.*`) lo decidono le tuple globali del gateway, e lì si verificano.
Qui restano le dichiarazioni che il seed porta ancora: le capability.

Testo originale:

clodia-platform#104 §10.2, ordine dell'owner del 2 ago 2026: «togli
`add_participant` a tutti tranne che a clodia, ma gated».

Perché un test e non solo la modifica dei seed: #102 ha misurato che
l'espansione della composizione — non i lati della trifecta — è la leva
dominante sul punteggio dei canali (canali 3/3 da 87 a 17 nello scenario senza
espansione). Un `topic.*` reintrodotto per comodità in un seed rimetterebbe
`topic.add_participant` in circolo **senza che nessuno se ne accorga**, perché
il wildcard non nomina il verbo che concede. Questo test lo nomina.
"""
from __future__ import annotations

import unittest

import yaml

from ..config import workspace_path
from .models import AgentSpec

SEEDS_DIR = workspace_path("catalogs/packs/base-pack/agents")


def _seeds() -> dict[str, AgentSpec]:
    out = {}
    for d in sorted(SEEDS_DIR.iterdir()):
        f = d / "agent.yaml"
        if f.is_file():
            out[d.name] = AgentSpec.model_validate(
                yaml.safe_load(f.read_text(encoding="utf-8")))
    return out


class AddParticipantTests(unittest.TestCase):

    def test_segretario_keeps_the_team_composition_skill(self) -> None:
        """Il segretario PROPONE la squadra (skill `team-composition`, verbo
        read-only `topic.suggest_team`): la capability resta nel seed."""
        self.assertIn("base-pack/team-composition", _seeds()["segretario"].capabilities)


class GrantHygieneTests(unittest.TestCase):

    def test_the_postman_is_retired(self) -> None:
        """Il messaggero non c'è più (clodia-platform#531): la posta entra col
        corriere, che è codice, e si risponde con `channel.deliver` (pavimento
        della stanza dal 27 ott 2026, #626)."""
        self.assertNotIn("messaggero", _seeds())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
