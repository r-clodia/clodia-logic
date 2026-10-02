"""`topic.link_add`: a chi va, e cosa deve leggere chi ce l'ha.

clodia-platform#477. Collegare due topic fa vedere a OGNI partecipante di
ciascuno i file dell'altro, in sola lettura.

Qui si difendono le due metà che stanno in questo repo: **chi** ha il verbo, e
**cosa** gli viene insegnato.

Sul «chi» l'invariante non è un elenco di nomi ma una regola: il collegamento va
solo ai **coordinatori dichiarati**, cioè `coordinator.DECLARED`. Non è una
regola scelta per far quadrare un caso: collegare due stanze è un atto di
coordinamento fra stanze — decide dove un canale va a prendere i documenti su
cui lavora — e il coordinatore è anche chi parla con l'owner che poi approva la
card. La lista sta in un posto solo e si cambia dove si vede; qui si legge, non
si ricopia, così la regola regge anche se la ruling cambia.

(Fino all'1 ott 2026 la regola era «dove c'è già `topic.add_participant`».
L'owner ha deciso che il verbo è del `segretario`, che quel grant non ha e non
deve avere: far entrare una stanza intera nel campo visivo di un'altra è un
atto di coordinamento, far entrare un agente in una stanza è un invito. Sono
due poteri diversi, e la vecchia regola li teneva legati senza che niente lo
imponesse.)

Sul «cosa»: il testo va dove l'agente che ha il verbo lo legge davvero. Per il
`segretario` **non è la skill** — non ha `base-pack/topic-files` — ma il suo
mandato, che per il resto gli prescrive di rifiutare in una riga tutto ciò che
non è scrittura di stato. Una capacità nuova concessa nello `agent.yaml` e non
scritta nel mandato lascia il seed a posto sulla carta e l'agente che rifiuta
lo stesso: è il caso di `test_move_file_grant`, qui con un secondo bersaglio —
la **rule** che quel seed porta in contesto, che non deve dichiarare fuori
dominio ciò che il grant gli consente.

La skill resta controllata a parte, ma per un'altra ragione: la legge chi *vede*
un mount collegato, che è ogni partecipante dei due topic, non solo chi ha
creato il collegamento.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from ..config import workspace_path
from .coordinator import DECLARED
from .inheritance import effective_tool_permissions

PACK = Path(workspace_path("catalogs/packs/base-pack"))
AGENTS = PACK / "agents"
PLUGINS = PACK / "plugins" / "base-pack"
SKILL = PLUGINS / "skills" / "topic-files" / "SKILL.md"
RULES = PLUGINS / "rules"

VERBO = "topic.link_add"


def _seeds() -> dict:
    out = {}
    for d in sorted(AGENTS.iterdir()):
        f = d / "agent.yaml"
        if f.is_file():
            out[d.name] = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return out


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.seeds = _seeds()
        self.grants = {n: set(effective_tool_permissions(n, self.seeds))
                       for n in self.seeds}
        self.holders = [n for n, g in self.grants.items() if VERBO in g]


class GrantTests(_Base):
    def test_link_goes_only_to_declared_coordinators(self) -> None:
        for nome in self.holders:
            with self.subTest(seed=nome):
                self.assertIn(
                    nome, DECLARED,
                    f"{nome} ha {VERBO} senza essere un coordinatore dichiarato "
                    f"({', '.join(DECLARED)}): collegare due stanze decide dove "
                    "un canale va a prendere i documenti su cui lavora, ed è "
                    "una scelta di coordinamento, non di mestiere")

    def test_whoever_can_link_can_unlink(self) -> None:
        """Un seed che propone collegamenti e non può ritirarli produce
        perimetri che crescono soltanto, e lascia all'owner una sola direzione."""
        for nome in self.holders:
            with self.subTest(seed=nome):
                self.assertIn("topic.link_remove", self.grants[nome])

    def test_at_least_one_seed_actually_has_it(self) -> None:
        """La regola sopra è vera anche se nessuno ha il verbo: senza questo
        controllo, una capacità dichiarata e concessa a nessuno passerebbe per
        un invariante rispettato."""
        self.assertTrue(self.holders,
                        "nessun seed ha topic.link_add: il verbo esiste nel "
                        "gateway e non lo può chiedere nessuno")


class MandatoTests(_Base):
    """Il testo deve arrivare a chi ha il verbo, non solo esistere nel pack."""

    def test_the_holder_mandate_names_the_verb(self) -> None:
        for nome in self.holders:
            with self.subTest(seed=nome):
                prompt = (AGENTS / nome / "system-prompt.md")
                self.assertTrue(prompt.is_file(), f"{nome} non ha un mandato")
                testo = prompt.read_text(encoding="utf-8").lower()
                self.assertIn(
                    VERBO, testo,
                    f"{nome} ha {VERBO} ma il suo mandato non lo nomina: un "
                    "grant che il mandato ignora produce un agente che rifiuta "
                    "una richiesta che avrebbe potuto servire")

    def test_the_holder_mandate_says_the_mount_is_read_only(self) -> None:
        """La metà che un agente sbaglierebbe da solo: il mount è in sola
        lettura, e aggirarla copiando il file di qua è proprio ciò che il
        collegamento serve a non fare."""
        for nome in self.holders:
            with self.subTest(seed=nome):
                testo = (AGENTS / nome / "system-prompt.md").read_text(
                    encoding="utf-8").lower()
                self.assertIn("sola lettura", testo)

    def test_no_rule_of_the_holder_declares_the_verb_out_of_domain(self) -> None:
        """Una rule che circoscrive il mestiere di un seed viaggia nel suo
        contesto accanto al mandato: se enumera il dominio consentito e il
        collegamento non c'è, l'agente rifiuta per obbedienza — grant o no.
        (Si leggono le rule dichiarate dal seed stesso; nessun seed con questo
        verbo ne eredita altre.)"""
        for nome in self.holders:
            for rule in (self.seeds[nome].get("rules") or []):
                f = RULES / f"{rule}.md"
                if not f.is_file():
                    continue
                testo = f.read_text(encoding="utf-8").lower()
                if "dominio consentito" not in testo:
                    continue
                with self.subTest(seed=nome, rule=rule):
                    self.assertIn(
                        "colleg", testo,
                        f"la rule `{rule}` enumera il dominio di {nome} e non "
                        "nomina il collegamento fra stanze: il mandato glielo "
                        "concede e la rule glielo toglie")


class SkillTests(unittest.TestCase):
    """La skill `topic-files` la legge chi *vede* un mount collegato — ogni
    partecipante dei due topic — non chi ha il verbo."""

    def test_the_skill_says_the_linked_mount_is_read_only(self) -> None:
        testo = SKILL.read_text(encoding="utf-8").lower()
        self.assertIn("sola lettura", testo)
        self.assertIn("collegato", testo)

    def test_the_skill_says_the_label_travels_with_the_file(self) -> None:
        """«Viene da un canale amico» non ripulisce un allegato di terzi."""
        testo = SKILL.read_text(encoding="utf-8").lower()
        self.assertIn("untrusted", testo)


if __name__ == "__main__":
    unittest.main()
