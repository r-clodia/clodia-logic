"""I seed del base-pack dichiarano il mestiere, non il pavimento.

Osservazione di Davide, 8 ago 2026: «lo yaml del segretario ripete verbi che gli
devono derivare da archseed, ma archseed non risulta essere un suo parent».

Due difetti in una frase, e sono diversi.

**I verbi ripetuti.** Quattro seed su cinque elencavano gli otto verbi base
dell'arciseed. Non è ridondanza innocua: se un seed ripete ciò che fanno tutti,
la domanda «cosa fa questo agente» resta senza risposta, sepolta sotto il
pavimento. `segretario` dichiarava tre verbi di cui due base — il suo mestiere
era **un** verbo, e non si vedeva.

**L'antenato non dichiarato.** Nel gateway `archseed` è antenato implicito di
tutti, e lo resta: un seed non deve poterne uscire omettendolo. Ma implicito non
è una ragione per essere invisibile — chi legge `segretario` non aveva modo di
sapere da dove venissero quei verbi. Ora la relazione è scritta, e il file dice
la verità che il gateway impone comunque.

Il test che conta è l'ultimo: **nessun seed può ridichiarare un verbo del
pavimento**. La ridondanza torna da sola alla prima modifica a mano, e nessuno se
ne accorgerebbe, perché non rompe niente — peggiora solo la leggibilità, che è
esattamente il tipo di difetto che nessun test coglie mai.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from ..config import workspace_path

AGENTS = Path(workspace_path("catalogs/packs/base-pack/agents"))


def _seeds() -> dict:
    out = {}
    for d in sorted(AGENTS.iterdir()):
        f = d / "agent.yaml"
        if f.is_file():
            out[d.name] = yaml.safe_load(f.read_text()) or {}
    return out


class ArchseedTests(unittest.TestCase):
    def test_the_archseed_is_in_the_pack(self):
        self.assertIn("archseed", _seeds())

    def test_it_is_abstract(self):
        self.assertTrue(_seeds()["archseed"].get("abstract"))

    def test_it_declares_no_engine(self):
        """Un antenato non gira: dichiarare un provider suggerirebbe che
        qualcosa possa eseguirlo."""
        a = _seeds()["archseed"]
        for campo in ("model", "providers", "agent_sdk"):
            with self.subTest(campo=campo):
                self.assertNotIn(campo, a)


class TradeTests(unittest.TestCase):
    def test_every_seed_declares_the_archseed_as_an_ancestor(self):
        for nome, y in _seeds().items():
            if nome == "archseed":
                continue
            with self.subTest(seed=nome):
                self.assertIn("archseed", y.get("parents") or [],
                              f"'{nome}' non dichiara da dove vengono i suoi verbi base")

    def test_no_seed_repeats_a_floor_verb(self):
        """Il test che impedisce alla ridondanza di tornare. Non rompe niente —
        peggiora solo la leggibilità, ed è per questo che senza un test
        tornerebbe."""
        base = set(_seeds()["archseed"]["tool_permissions"])
        for nome, y in _seeds().items():
            if nome == "archseed":
                continue
            ripetuti = sorted(set(y.get("tool_permissions") or []) & base)
            with self.subTest(seed=nome):
                self.assertEqual(
                    ripetuti, [],
                    f"'{nome}' ridichiara verbi del pavimento: arrivano già "
                    f"dall'arciseed, e ripeterli seppellisce il suo mestiere")

    def test_a_seed_still_declares_something_of_its_own(self):
        """Un seed che dopo la pulizia non dichiara niente non è un seed: è
        l'arciseed con un nome diverso."""
        for nome, y in _seeds().items():
            if nome == "archseed":
                continue
            with self.subTest(seed=nome):
                if nome == "ophelia":
                    self.assertEqual(y.get("tool_permissions"), [])
                    continue
                self.assertTrue(y.get("tool_permissions"),
                                f"'{nome}' non dichiara alcun mestiere proprio")

    def test_ophelia_declares_no_extra_trade_but_inherits_the_floor(self):
        seeds = _seeds()
        self.assertEqual(seeds["ophelia"].get("tool_permissions"), [])
        from .inheritance import effective_tool_permissions
        effective = effective_tool_permissions("ophelia", seeds)
        self.assertIn("topic.post_message", effective)
        self.assertIn("memory.*", effective)
        self.assertNotIn("*", effective)


if __name__ == "__main__":
    unittest.main()


class AncestryTests(unittest.TestCase):
    """`parents` è una relazione di AUTORITÀ, non genealogia.

    Le due cose erano confuse finché nessuno risolveva il campo. Il giorno in cui
    è diventato portante — 8 ago 2026 — `sysadmin` si sarebbe preso **33 verbi**
    da `parents: [clodia]`, una riga scritta quando non significava niente. E
    `clodia-primal`, che come seed non esiste, non concedeva nulla ma era una
    mina: il giorno in cui qualcuno crea un seed con quel nome, tre agenti si
    allargano da soli e nessuno collega le due cose.
    """

    def test_every_declared_parent_exists(self):
        """Un antenato che non esiste oggi è un permesso che arriva domani."""
        seeds = _seeds()
        for nome, y in seeds.items():
            for g in (y.get("parents") or []):
                with self.subTest(seed=nome, parent=g):
                    self.assertIn(g, seeds,
                                  f"'{nome}' dichiara l'antenato '{g}', che non "
                                  f"esiste: oggi non concede niente, e il giorno "
                                  f"in cui quel seed viene creato concede tutto")

    def test_no_seed_inherits_from_another_working_seed(self):
        """Non è vietato in generale — un `professionista` con sotto `avvocato` e
        `commercialista` è il caso d'uso della voce 10. Ma nel base-pack di oggi
        nessuna di queste relazioni è stata decisa: erano genealogia, e lasciarle
        significa concedere per eredità ciò che nessuno ha concesso."""
        seeds = _seeds()
        for nome, y in seeds.items():
            altri = [g for g in (y.get("parents") or []) if g != "archseed"]
            with self.subTest(seed=nome):
                self.assertEqual(
                    altri, [],
                    f"'{nome}' eredita da {altri}: se è voluto, va deciso e "
                    f"misurato — quanti verbi arrivano di lì?")


class ClodiaMandateTests(unittest.TestCase):
    """Il mandato di clodia: sei namespace di verbi, tre pack, e nulla d'altro.

    agents-notebook A6 e A7. Erano rimaste senza test automatico fino al 16 ago
    2026, e sono esattamente il tipo di requisito che si sfalda in silenzio: un
    verbo aggiunto «già che ci siamo» non rompe niente, e nessuno se ne accorge
    finché non si rilegge il seed riga per riga.
    """

    #: A6: «mantiene tutti i verbi topic, artifact, agents, memory, fs e github.
    #: Perde tutti gli altri». `web.fetch` ed `email.send` sono l'eccezione
    #: decisa il 15 ago (A12 non li tocca, li aggiunge il pack 7.9.0): stanno
    #: qui perché l'elenco dica la verità, non perché A6 sia stata allargata.
    NAMESPACE_AMMESSI = {"topic", "artifact", "agents", "memory", "fs", "github",
                         "runtime", "integrations", "providers", "mcp", "packs",
                         "jobs", "egress", "ingress", "rag", "web", "email"}

    #: A7: «confermiamo base, editorial, e anthropic. Perde comms»
    PACK_ATTESI = {"base-pack", "editorial-pack", "anthropic-pack"}

    def setUp(self) -> None:
        self.clodia = _seeds()["clodia"]

    def test_no_verb_outside_the_declared_namespaces(self) -> None:
        fuori = sorted(
            v for v in (self.clodia.get("tool_permissions") or [])
            if v.split(".", 1)[0] not in self.NAMESPACE_AMMESSI
        )
        self.assertEqual([], fuori, f"verbi fuori dai namespace di A6: {fuori}")

    def test_the_verbs_are_enumerated_not_a_wildcard(self) -> None:
        """«clodia non può più avere [*]» (6 ago). Il wildcard non era pericoloso
        per ciò che concedeva allora: lo era perché avrebbe concesso in automatico
        ogni verbo aggiunto dopo, senza che nessuno lo valutasse."""
        verbi = self.clodia.get("tool_permissions") or []
        self.assertNotIn("*", verbi)
        self.assertTrue(verbi, "clodia senza verbi: il seed non dichiara più il mestiere")

    def test_comms_pack_is_gone_and_the_other_three_stay(self) -> None:
        """A7 consegnata (clodia-platform#198, 17 ago 2026): `comms-pack` non è
        più fra le capabilities di clodia — la posta è del corriere.

        Questo test è nato ROSSO, con `@unittest.expectedFailure` e il numero
        della issue accanto: era la forma in cui un requisito non implementato si
        vedeva, invece di restare indistinguibile da uno consegnato (decision
        record 34). Ora che il seed è stato corretto l'`expectedFailure` è
        sparito e il test è diventato la GUARDIA del requisito: il wildcard
        `comms-pack/*` tornerebbe da sé alla prima modifica a mano del seed, e
        senza questa riga rientrerebbe in silenzio, esattamente come ci era
        rimasto per nove giorni.

        Le due asserzioni guardano lati opposti: la prima che comms sia andato,
        la seconda che gli altri tre pack ci siano ancora — perché «tolgo il
        wildcard di troppo» e «svuoto le capabilities» hanno lo stesso aspetto
        sulla prima asserzione da sola.
        """
        pack = {c.split("/", 1)[0] for c in (self.clodia.get("capabilities") or [])}
        self.assertNotIn("comms-pack", pack, "A7: comms se ne va — la posta è del corriere")
        self.assertLessEqual(self.PACK_ATTESI, pack)

    def test_rules_are_declared_one_by_one(self) -> None:
        """Non `["*"]`: il catalogo contiene una sola rule, del segretario, e il
        wildcard la imponeva a clodia — che per quattro mattine ha risposto «non
        rientra nel mio ambito» a un job suo (clodia-logic#290)."""
        self.assertNotIn("*", self.clodia.get("rules") or [])


class HumanContactFieldTests(unittest.TestCase):
    """Telegram è un recapito, quindi è un campo della persona.

    agents-notebook A10: «essendo telegram un metodo di contatto per gli umani lo
    metterei come campo fisso e non extra». Un recapito negli `extras` è una
    stringa che nessuno valida e che ogni lettore interpreta a modo suo.
    """

    def test_telegram_is_a_declared_field_of_the_spec(self) -> None:
        from .models import AgentSpec
        self.assertIn("telegram", AgentSpec.model_fields,
                      "A10: telegram è tornato un extra")

    def test_a_human_carries_it_outside_the_extras(self) -> None:
        from .models import AgentSpec
        s = AgentSpec.model_validate({
            "name": "davide", "description": "d", "display_name": "Davide",
            "type": "human", "role": "superadmin", "telegram": "76632169",
        })
        self.assertEqual("76632169", s.telegram)
        extras = getattr(s, "extras", None) or {}
        self.assertNotIn("telegram", extras,
                         "A10: il valore è finito anche negli extras — due fonti, una divergerà")
