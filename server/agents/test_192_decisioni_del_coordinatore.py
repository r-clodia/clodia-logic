"""Le decisioni di clodia-platform#192 sul segretario.

Dal 27 ott 2026 (clodia-platform#626) i seed non dichiarano verbi: «raccomanda,
non esegue» e il perimetro delle letture vivono nelle tuple globali del
gateway e nei gate WALLS, e lì si verificano. Qui resta la parte di testo del
mandato, che è nel seed.

Testo originale:

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

from ..config import workspace_path

AGENTS = Path(workspace_path("catalogs/packs/base-pack/agents"))


class IlSegretarioRaccomandaNonEsegueTests(unittest.TestCase):
    """Decisione 1 di #192: il mandato propone, non invita."""

    def test_il_mandato_di_coordinamento_propone_e_non_invita(self) -> None:
        testo = (AGENTS / "segretario" / "system-prompt.md").read_text(
            encoding="utf-8")
        self.assertIn("[COORDINAMENTO]", testo,
                      "il mandato non legge il quarto summon kind")
        self.assertIn("<!-- invite=", testo,
                      "senza il marker il terzo esito non ha una forma: "
                      "l'owner non riceve nessun bottone da premere")


if __name__ == "__main__":
    unittest.main()
