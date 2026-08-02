"""Chi può ALLARGARE la composizione di un canale, fra i seed del base-pack.

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
from . import trifecta
from .models import AgentSpec

SEEDS_DIR = workspace_path("catalogs/packs/base-pack/agents")

#: L'unico seed a cui l'owner ha lasciato la composizione delle squadre. Il
#: verbo resta comunque `gated` nel gateway (`clodia-tools/server/gate.py`,
#: `_DEFAULT_GATED_EXACT`), quindi ogni invito passa da una conferma umana.
COMPOSER = "clodia"

#: Seed che restano `super` e bypassano la whitelist per-agente
#: (`_SUPER_AGENTS` in clodia-tools `whitelist.py` e `main.py`): per loro il
#: contenuto di `tool_permissions` non è enforced, quindi togliere il grant non
#: avrebbe alcun effetto. Vedi il PR di §10.2 e #104 §8 («ophelia: candidata a
#: uscire dal base-pack»).
SUPER_BYPASS = {"clodia", "ophelia"}


def _seeds() -> dict[str, AgentSpec]:
    out = {}
    for d in sorted(SEEDS_DIR.iterdir()):
        f = d / "agent.yaml"
        if f.is_file():
            out[d.name] = AgentSpec.model_validate(
                yaml.safe_load(f.read_text(encoding="utf-8")))
    return out


def _grants(spec: AgentSpec) -> list[str]:
    return [str(g).strip() for g in (spec.tool_permissions or []) if str(g).strip()]


class AddParticipantTests(unittest.TestCase):

    def test_only_the_composer_is_granted_add_participant(self) -> None:
        for name, spec in _seeds().items():
            if name in SUPER_BYPASS:
                continue  # coperto da test_super_agents_bypass_the_grant_anyway
            granted = any(trifecta._overlap(g, "topic.add_participant")
                          for g in _grants(spec))
            with self.subTest(seed=name):
                self.assertFalse(
                    granted,
                    f"'{name}' concede topic.add_participant (spesso via un "
                    f"`topic.*` reintrodotto): solo '{COMPOSER}' deve comporre squadre")

    def test_the_composer_keeps_it(self) -> None:
        # L'altra metà dell'ordine: non è una rimozione totale. Se un domani
        # sparisse anche a clodia, nessuno potrebbe più comporre una squadra.
        spec = _seeds()[COMPOSER]
        self.assertTrue(any(trifecta._overlap(g, "topic.add_participant")
                            for g in _grants(spec)))

    def test_no_seed_reintroduces_the_topic_wildcard(self) -> None:
        """`topic.*` è la forma in cui il verbo rientra senza essere nominato.

        È così che ci era arrivato: nessun seed elencava `topic.add_participant`
        esplicitamente — otto ce l'avevano tutti via wildcard (#102).
        """
        for name, spec in _seeds().items():
            if name in SUPER_BYPASS:
                continue
            with self.subTest(seed=name):
                self.assertNotIn("topic.*", _grants(spec))


class ExpansionClosureTests(unittest.TestCase):
    """`add_participant` non è l'unico verbo che allarga la composizione: il
    catalogo trifecta conta come `expansion` anche `agents.*`."""

    def test_messaggero_and_segretario_can_no_longer_expand(self) -> None:
        seeds = _seeds()
        for name in ("messaggero", "segretario"):
            with self.subTest(seed=name):
                self.assertFalse(trifecta.agent_profile(seeds[name])["expands"])

    def test_sysadmin_still_expands_through_agents_wildcard(self) -> None:
        """Caveat misurato, non dimenticanza.

        Togliere `add_participant` a sysadmin non lo rende incapace di allargare
        la composizione: conserva `agents.*` (grant/revoke di capability =
        fabbricare autorità), che il catalogo classifica come `expansion`. Per
        sysadmin il §10.2 si completa solo con lo split di `agents.*` previsto in
        #104 §8. Il test è scritto in positivo perché la situazione cambi
        DELIBERATAMENTE: quando `agents.*` verrà spezzato, questo fallisce e
        obbliga ad aggiornare il modello invece di lasciarlo invecchiare.
        """
        spec = _seeds()["sysadmin"]
        profile = trifecta.agent_profile(spec)
        self.assertTrue(profile["expands"])
        lit = trifecta._matching_grants(_grants(spec),
                                        trifecta.load_config()["expansion"])
        self.assertEqual(lit, ["agents.*"])
        self.assertNotIn("topic.add_participant", lit)

    def test_super_agents_bypass_the_grant_anyway(self) -> None:
        """Per clodia e ophelia `tool_permissions` non è enforced.

        `_SUPER_AGENTS` in clodia-tools (`whitelist.py:283`, `main.py:1823`)
        short-circuita la whitelist prima del match dei grant. Togliere il verbo
        dal seed di ophelia sarebbe **teatro**: non cambierebbe nulla a runtime.
        Il test documenta il limite del §10.2 così com'è ordinato — l'unico modo
        di toglierlo a ophelia è farla uscire dai super (#104 §8).
        """
        for name in sorted(SUPER_BYPASS):
            with self.subTest(seed=name):
                self.assertTrue(trifecta.agent_profile(_seeds()[name])["expands"])


class GrantHygieneTests(unittest.TestCase):

    def test_exploded_lists_contain_no_wildcard_topic_verb(self) -> None:
        # Un'esplosione fatta male (`topic.add_*`) non matcherebbe nulla nella
        # RBAC del gateway, che conosce solo `ns.*` e il verbo esatto.
        for name, spec in _seeds().items():
            for grant in _grants(spec):
                if grant.startswith("topic.") and grant.endswith("*"):
                    with self.subTest(seed=name, grant=grant):
                        self.assertEqual(grant, "topic.*")

    def test_every_seed_still_parses_and_keeps_its_other_grants(self) -> None:
        # Guardia grossolana contro un'esplosione che perde per strada dei verbi:
        # i due seed toccati devono restare largamente più ricchi di prima.
        seeds = _seeds()
        self.assertGreater(len(_grants(seeds["messaggero"])), 20)
        self.assertGreater(len(_grants(seeds["sysadmin"])), 40)
        for name in ("email.*", "telegram.*", "jobs.propose"):
            self.assertIn(name, _grants(seeds["messaggero"]))
        for name in ("agents.*", "settings.*", "web.post"):
            self.assertIn(name, _grants(seeds["sysadmin"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
