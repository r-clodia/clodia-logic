"""Le due decisioni di clodia-platform#192 che vivono nei grant del segretario.

La issue ne lasciava aperte tre. Misurate sul codice, tutte e tre hanno già una
risposta — ma due sono vere **per costruzione**, cioè per come i grant sono stati
scritti, e nessuna riga le nomina. Una decisione che nessun test nomina si perde
al primo refactor che sembra innocuo: chi un domani vorrà «aiutare il segretario
a coordinare meglio» gli concederà il verbo che serve, e nessuno gli dirà che
quella era una decisione dell'owner e non una svista.

1. **Azione del segretario** (R16, «riferire»): raccomanda, non esegue. Il modo
   in cui la decisione è implementata *è* l'assenza del verbo — la skill
   `team-composition` chiude con `<!-- invite=… -->` e l'invito lo fa l'owner col
   bottone. Decisione dell'owner del 6 set 2026, già registrata per la parte di
   testo in `test_seed_mandates.py`; qui si blocca la parte di permessi.
2. **Perimetro delle letture**: metadata del topic e stato pubblico dei
   partecipanti, mai memoria o stato privato degli altri agenti.

La terza (il fallimento del bot raccomandato: stop dopo un handoff, nessuna
seconda cascata) non sta qui perché non sta nei grant: è garantita da
`_maybe_delegate`, che riparte SOLO su un `@`, e dal tetto `_MAX_DELEGATION_HOPS`
— che dopo R16 il canale lo annuncia invece di tacere. È già coperta da
`test_delegation_limit_visible.py`.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from ..config import workspace_path
from .inheritance import effective_tool_permissions

AGENTS = Path(workspace_path("catalogs/packs/base-pack/agents"))

#: Il perimetro deciso: letture confinate allo scope corrente, la propria
#: memoria, e le sole scritture del mestiere. Non è un elenco di ciò che il seed
#: ha oggi — è ciò che gli è stato CONCESSO di avere. Aggiungere una riga qui è
#: il gesto che richiede la decisione, ed è per questo che il test guarda un
#: elenco scritto a mano invece di rileggere lo stesso file che sta misurando.
PERIMETRO = {
    # letture dello scope in cui lo spawn sta
    "topic.open", "topic.files", "topic.read_file", "topic.read_document",
    "topic.search", "topic.list", "topic.fetch",
    # parlare nella propria stanza
    "topic.post_message",
    # il mestiere: lo stato scritto del topic
    "topic.save_summary",
    # la proposta di squadra, read-only, che NON invita
    "topic.suggest_team",
    # la propria memoria, confinata alla propria cartella
    "memory.*",
}

#: Verbi che eseguirebbero al posto di raccomandare. `add_participant` è quello
#: che la issue nomina; gli altri sono la stessa decisione presa da una porta
#: laterale — invitare, rimuovere, o costruire la stanza.
ESECUZIONE = {
    "topic.add_participant", "topic.remove_participant", "topic.invite",
    "topic.new", "topic.archive", "topic.delete_file",
}


def _seeds() -> dict:
    out = {}
    for d in sorted(AGENTS.iterdir()):
        f = d / "agent.yaml"
        if f.is_file():
            out[d.name] = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return out


def _grants(name: str) -> set[str]:
    return {str(g) for g in effective_tool_permissions(name, _seeds())}


class IlSegretarioRaccomandaNonEsegueTests(unittest.TestCase):
    """Decisione 1 di #192, e la porta laterale che la aggirerebbe."""

    def test_non_ha_nessun_verbo_che_esegue_al_posto_di_raccomandare(self) -> None:
        concessi = _grants("segretario")
        esegue = sorted(v for v in ESECUZIONE
                        if v in concessi or f"{v.split('.')[0]}.*" in concessi
                        or "*" in concessi)
        self.assertEqual(
            [], esegue,
            "il segretario può eseguire invece di raccomandare: R16 dice "
            "«riferire», e l'owner il 6 set 2026 ha scelto il marker di invito "
            f"col bottone. Verbi di troppo: {esegue}")

    def test_la_proposta_di_squadra_resta_la_strada(self) -> None:
        """Il rovescio: tolto il verbo, deve restare il modo di raccomandare.

        Senza questa metà il test sopra si accontenterebbe di un seed a cui
        hanno tolto tutto — che passa, e non sa più fare il terzo esito.
        """
        self.assertIn("topic.suggest_team", _grants("segretario"))

    def test_il_mandato_di_coordinamento_propone_e_non_invita(self) -> None:
        testo = (AGENTS / "segretario" / "system-prompt.md").read_text(
            encoding="utf-8")
        self.assertIn("[COORDINAMENTO]", testo,
                      "il mandato non legge il quarto summon kind")
        self.assertIn("<!-- invite=", testo,
                      "senza il marker il terzo esito non ha una forma: "
                      "l'owner non riceve nessun bottone da premere")


class IlPerimetroDelleLettureTests(unittest.TestCase):
    """Decisione 3 di #192: coordinare non allarga ciò che si può leggere."""

    def test_nessun_verbo_fuori_dal_perimetro_deciso(self) -> None:
        fuori = sorted(_grants("segretario") - PERIMETRO)
        self.assertEqual(
            [], fuori,
            "il segretario legge fuori dal perimetro deciso in #192 (metadata "
            "del topic e stato pubblico dei partecipanti, mai memoria o stato "
            f"privato altrui): {fuori}")

    def test_la_memoria_resta_la_propria(self) -> None:
        """`memory.*` è ampio sul namespace e stretto sui dati: il gateway lo
        confina alla cartella del seed. Se un giorno comparisse un verbo di
        memoria con un bersaglio, il perimetro sopra lo vedrebbe — questo test
        dice perché la wildcard, da sola, non è una violazione."""
        memoria = {g for g in _grants("segretario") if g.startswith("memory")}
        self.assertEqual({"memory.*"}, memoria)


if __name__ == "__main__":
    unittest.main()
