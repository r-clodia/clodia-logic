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

import unittest
from pathlib import Path

from ..config import workspace_path

AGENTS = Path(workspace_path("catalogs/packs/base-pack/agents"))

# `MandateNamesOnlyGrantedVerbsTests` checked each prompt against the seed's
# `tool_permissions`. Since #626 the seeds carry no verbs (the check had become
# vacuous: no namespace, no verb named), and #656 removed the field: what an
# agent may call is the gateway's A·V·R·Q tuples, which a repository test
# cannot read. The checks on the secretary's mandate below stay.


def _prompt(name: str) -> str:
    return (AGENTS / name / "system-prompt.md").read_text(encoding="utf-8")


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
