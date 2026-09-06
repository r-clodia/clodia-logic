"""Il mandato di un seed non può ordinare verbi che il gate gli nega.

clodia-platform#196 (A5). La issue chiedeva di dare al segretario «i due verbi
per convocare una squadra», e la misura ha ridotto lo scope: `topic.suggest_team`
il seed ce l'ha già, e `topic.add_participant` non gli serve — la skill
`team-composition` chiude con `<!-- invite=… -->` e l'invito lo esegue l'owner
col bottone. Restava un difetto vero, e stava **nel testo**: convocato come
coordinatore, il mandato gli ordinava di rispondere «Fuori dominio: chiedi al
capitano» in una stanza dove il capitano è lui.

Cercando quella riga ne è venuta fuori una seconda della stessa famiglia: la
sezione «Cosa fai» prometteva `topic.add_minute` e `topic.write_file`, che
clodia-platform#212 gli ha tolto il 5 ago 2026 («a redactor that can write
arbitrary files is not a redactor»). Un mandato che nomina un tool inesistente
non è un refuso: è un modello che spende il turno a chiamarlo, riceve un rifiuto
del gate, e non fa il lavoro che gli era stato chiesto.

Il primo test è quindi scritto su TUTTI i seed del pack, non sul solo segretario:
i due difetti sono lo stesso difetto, e nascono nello stesso modo — una revoca di
verbi che tocca lo `agent.yaml` e dimentica il `system-prompt.md` accanto. Con la
sola dichiarazione corretta il seed sembra a posto e l'agente sbaglia lo stesso.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

from ..config import workspace_path
from .inheritance import effective_tool_permissions

AGENTS = Path(workspace_path("catalogs/packs/base-pack/agents"))

#: `ns.verbo` scritto nel mandato. Il filtro sul namespace lo fa il chiamante,
#: che conosce i namespace realmente esistenti: senza, `summary.md` sarebbe un
#: verbo e ogni nome di file una violazione.
_VERB_RE = re.compile(r"\b([a-z][a-z_]*)\.([a-z_][a-z0-9_]*)\b")


def _seeds() -> dict:
    out = {}
    for d in sorted(AGENTS.iterdir()):
        f = d / "agent.yaml"
        if f.is_file():
            out[d.name] = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return out


def _prompt(name: str) -> str:
    return (AGENTS / name / "system-prompt.md").read_text(encoding="utf-8")


def _namespaces(seeds: dict) -> set[str]:
    """I namespace che ESISTONO davvero, letti dai verbi concessi ai seed.

    Scritti a mano invecchierebbero al primo namespace nuovo: il test smetterebbe
    di guardare proprio la parte di piattaforma appena aggiunta, in silenzio.
    """
    out = set()
    for name in seeds:
        for grant in effective_tool_permissions(name, seeds):
            ns = str(grant).split(".", 1)[0]
            if ns and ns != "*":
                out.add(ns)
    return out


def _verbs_named_in(text: str, namespaces: set[str]) -> set[str]:
    return {m.group(0) for m in _VERB_RE.finditer(text)
            if m.group(1) in namespaces}


def _granted(verb: str, grants: list[str]) -> bool:
    ns = verb.split(".", 1)[0]
    return verb in grants or f"{ns}.*" in grants or "*" in grants


class MandateNamesOnlyGrantedVerbsTests(unittest.TestCase):

    def test_no_seed_orders_a_verb_its_own_gate_denies(self) -> None:
        seeds = _seeds()
        ns = _namespaces(seeds)
        for name in seeds:
            if not (AGENTS / name / "system-prompt.md").is_file():
                continue
            grants = effective_tool_permissions(name, seeds)
            promessi = sorted(v for v in _verbs_named_in(_prompt(name), ns)
                              if not _granted(v, grants))
            with self.subTest(seed=name):
                self.assertEqual(
                    [], promessi,
                    f"il mandato di '{name}' ordina verbi che il gate nega: "
                    f"{promessi}. Se il verbo serve, va aggiunto ai "
                    f"`tool_permissions`; se non serve, il mandato non deve "
                    f"nominarlo — un tool inesistente non è un refuso, è un "
                    f"turno speso in una chiamata che verrà rifiutata.")


class SecretaryConveningMandateTests(unittest.TestCase):
    """A5: convocato come coordinatore, il segretario deve DECIDERE.

    La rule `topic-state-boundary` l'eccezione ce l'ha già scritta; il mandato
    del seed diceva ancora il contrario, e fra i due vince quello che l'agente
    legge per primo — sono nello stesso prompt, e uno dei due è sbagliato.
    """

    def setUp(self) -> None:
        self.prompt = _prompt("segretario")

    def _coordination_section(self) -> str:
        """Il testo della sezione che governa il turno di coordinamento.

        Si guarda la SEZIONE e non l'intero file perché `topic.suggest_team` e il
        marker di invito nel mandato ci sono già — nella sezione di bootstrap. Un
        `assertIn` sul file intero sarebbe verde oggi, cioè misurerebbe la
        sezione sbagliata e non vedrebbe mai il difetto che deve trovare.
        """
        dopo = self.prompt.split("[COORDINAMENTO]", 1)
        self.assertEqual(2, len(dopo),
                         "il mandato non nomina la direttiva di coordinamento")
        return dopo[1].split("\n## ", 1)[0]

    def test_the_mandate_reads_the_coordination_directive(self) -> None:
        self.assertIn("[COORDINAMENTO]", self.prompt)

    def test_the_fourth_outcome_proposes_a_team_instead_of_refusing(self) -> None:
        """Il quarto esito è il punto della issue.

        Gli altri tre — è tuo, è di un altro, non c'è nessuno — li dà già la
        direttiva. Quello che mancava è il caso in cui la stanza non basta ma la
        colonia sì: lì il segretario propone la squadra, esattamente come al
        bootstrap, e non «lo dice e basta».
        """
        sezione = self._coordination_section()
        self.assertIn("topic.suggest_team", sezione)
        self.assertIn("<!-- invite=", sezione)

    def test_the_out_of_domain_refusal_is_no_longer_unconditional(self) -> None:
        """«Fuori dominio: chiedi al capitano» detto DAL capitano è un vicolo cieco.

        Il test guarda dove la riga vive: la sezione che contiene il rifiuto deve
        nominare l'eccezione, altrimenti l'eccezione sta altrove nel file e il
        modello che legge quella riga non ha modo di saperlo.
        """
        sezione = self.prompt.split("## Cosa NON fai", 1)
        self.assertEqual(2, len(sezione), "sezione «Cosa NON fai» sparita")
        self.assertIn("COORDINAMENTO", sezione[1])

    def test_the_invite_stays_the_owner_s(self) -> None:
        """Strada A: la proposta è dell'agente, la mutazione dell'owner.

        La coppia dell'asserzione sopra: nominare il marker e nominare anche il
        verbo gated significherebbe aver scelto la strada B senza dirlo.
        """
        self.assertNotIn("topic.add_participant", self.prompt)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
