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

#: Seed che conserva deliberatamente la composizione delle squadre.
SUPER_BYPASS = {"clodia"}


def _seeds() -> dict[str, AgentSpec]:
    out = {}
    for d in sorted(SEEDS_DIR.iterdir()):
        f = d / "agent.yaml"
        if f.is_file():
            out[d.name] = AgentSpec.model_validate(
                yaml.safe_load(f.read_text(encoding="utf-8")))
    return out


def _grants(spec: AgentSpec) -> list[str]:
    """I verbi EFFETTIVI del seed: i propri più quelli ereditati.

    Dichiarati ed effettivi hanno smesso di coincidere l'8 ago 2026, quando i
    seed hanno cominciato a ereditare dall'arciseed. Questo file chiede «il
    mestiere non si perde per strada», e il mestiere non si perde se un verbo
    arriva da un antenato invece che dalla riga: guardare la sola dichiarazione
    farebbe fallire il test proprio quando la pulizia ha funzionato.
    """
    from .inheritance import effective_tool_permissions
    return effective_tool_permissions(getattr(spec, "name", ""), _seeds())


class AddParticipantTests(unittest.TestCase):

    def test_segretario_can_suggest_but_not_invite_the_bootstrap_team(self) -> None:
        spec = _seeds()["segretario"]
        grants = _grants(spec)
        self.assertIn("base-pack/team-composition", spec.capabilities)
        self.assertIn("topic.suggest_team", grants)
        self.assertFalse(any(trifecta._overlap(g, "topic.add_participant")
                             for g in grants))

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

    def test_segretario_can_no_longer_expand(self) -> None:
        self.assertFalse(trifecta.agent_profile(_seeds()["segretario"])["expands"])

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

    def test_clodia_keeps_expansion_by_design(self) -> None:
        for name in sorted(SUPER_BYPASS):
            with self.subTest(seed=name):
                self.assertTrue(trifecta.agent_profile(_seeds()[name])["expands"])

    def test_ophelia_has_no_wildcard_and_does_not_expand(self) -> None:
        """18 set 2026: ophelia ha guadagnato gdrive.* enumerato (decisione di
        Davide) — non più `tool_permissions: []`, ma niente wildcard e nessuna
        capacità di espandere la composizione del canale, come prima."""
        spec = _seeds()["ophelia"]
        self.assertTrue(spec.tool_permissions)
        self.assertTrue(all(g.startswith("gdrive.") for g in spec.tool_permissions))
        self.assertNotIn("*", _grants(spec))
        self.assertFalse(trifecta.agent_profile(spec)["expands"])


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
        """Un'esplosione dei verbi non deve perdere per strada il MESTIERE.

        Prima questo test fissava una soglia numerica (messaggero > 20 verbi).
        Il refactoring per classe di seed l'ha ridotto a 13 **di proposito** —
        misurato: 44 dei 45 rifiuti registrati erano suoi, perché la §8 gli aveva
        tolto il mestiere — quindi la soglia asseriva un fatto non più vero e
        restava rossa mentre il codice era giusto. Un test così maschera la
        regressione che dovrebbe trovare.

        Ora si asserisce il CONTENUTO: i verbi senza i quali l'agente non fa il
        proprio lavoro. Il numero può scendere ancora; questi non possono
        sparire.
        """
        seeds = _seeds()
        # clodia risponde fuori dalla stanza da quando il messaggero è stato
        # ritirato (clodia-platform#531): senza `channel.deliver` nessun seed
        # risponderebbe più a un gruppo Telegram collegato.
        for verb in ("channel.deliver", "email.send"):
            with self.subTest(seed="clodia", verb=verb):
                self.assertIn(verb, _grants(seeds["clodia"]))
        # sysadmin amministra: resta largo per natura del ruolo.
        self.assertGreater(len(_grants(seeds["sysadmin"])), 40)
        for verb in ("agents.*", "settings.*", "web.post"):
            with self.subTest(seed="sysadmin", verb=verb):
                self.assertIn(verb, _grants(seeds["sysadmin"]))

    def test_the_postman_is_retired(self) -> None:
        """Il messaggero non c'è più (clodia-platform#531): la posta entra col
        corriere, che è codice, e si risponde con `channel.deliver`.

        Il verbo d'uscita stava sul solo `clodia`, e il suo commento diceva che
        allargarlo era una decisione dell'owner. Presa il 24 ott 2026 su #607,
        e non per comodità: diventando `segretario` il coordinatore di ogni
        stanza, `channel_relay._room_can_answer` — che rifiuta il turno quando
        chi coordina non ha il verbo — avrebbe reso MUTA ogni stanza collegata
        a un gruppo Telegram o a una casella. I due seed che coordinano lo
        hanno entrambi; nessun altro, ed è quello che si misura qui.
        """
        seeds = _seeds()
        self.assertNotIn("messaggero", seeds)
        for nome in ("clodia", "segretario"):
            with self.subTest(seed=nome):
                self.assertIn("channel.deliver", _grants(seeds[nome]))
        altri = sorted(n for n, s in seeds.items()
                       if n not in ("clodia", "segretario")
                       and "channel.deliver" in _grants(s))
        self.assertEqual(
            [], altri,
            "l'uscita fuori dalla stanza è dei coordinatori dichiarati "
            f"(coordinator.DECLARED), non di chiunque: {altri}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
