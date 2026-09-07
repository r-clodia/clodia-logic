"""Il turno consegnato a mano dall'umano non può arrivare muto.

Coda di clodia-platform#360 («l'agente scelto dal router risponde *messaggio
vuoto*»). La parte comune — il percorso `plain`, cioè nessuna menzione e router
che sceglie — è stata chiusa dalla #367, che ha dato un mandato a quel `kind`.
Restava scoperto l'ULTIMO ramo che passa da `_start_turn`: `routed-choice`.

Ci si arriva in due modi, entrambi con una persona che ha deciso:

- il dialogo di disambiguazione del routing, quando l'utente clicca l'agente
  (`channels.py`, resolve della scelta di routing);
- lo scavalcamento del router, quando l'autore o l'owner dicono «no, questo».

In tutti e due i casi `_tag_directive("routed-choice", …)` cadeva sul `return
None` finale. Con `None`, il prompt del PRIMO turno è la sola storia del canale:
il messaggio dell'utente non c'è, e l'agente risponde di non vedere nessuna
richiesta. Esattamente il sintomo della issue, sul percorso in cui è più
antipatico — qui l'utente ha appena scelto quell'agente con un click.

L'altra metà del fix è la guardia in `test_plain_turn_directive`: la lista dei
`kind` non si scrive più a mano, si legge dai call-site di `_start_turn`. Questo
ramo mancante è la prova che una lista scritta a mano si dimentica di aggiornare.
"""
from __future__ import annotations

import unittest

from .channels import _tag_directive

_TESTO = "Puoi verificare la scadenza del contratto CPTO?"


class LaSceltaDellUmanoPortaIlMessaggio(unittest.TestCase):

    def test_routed_choice_non_e_piu_muto(self) -> None:
        """IL CASO DELLA ISSUE: era `None`, quindi prompt senza richiesta."""
        d = _tag_directive("routed-choice", "davide", _TESTO)
        self.assertIsNotNone(
            d, "senza direttiva il primo turno non contiene il messaggio umano")
        self.assertIn(_TESTO, d, "l'agente deve vedere la richiesta, non cercarla")

    def test_dice_che_a_sceglierlo_e_stata_una_persona(self) -> None:
        """È la differenza con `plain`: qui non ha deciso il router. Dirlo evita
        il rimbalzo «non credo di essere io il destinatario»."""
        d = _tag_directive("routed-choice", "davide", _TESTO).lower()
        self.assertIn("davide", d)
        self.assertIn("scelto te", d)

    def test_dice_che_il_turno_e_suo_e_che_nessun_altro_risponde(self) -> None:
        d = _tag_directive("routed-choice", "davide", _TESTO).lower()
        self.assertIn("turno è tuo", d)
        self.assertIn("nessun altro", d)

    def test_lascia_la_via_duscita_fuori_dominio(self) -> None:
        """Anche una scelta umana può sbagliare bersaglio: senza via d'uscita
        l'unico modo di obbedire sarebbe rispondere fuori dominio."""
        d = _tag_directive("routed-choice", "davide", _TESTO).lower()
        self.assertIn("dominio", d)
        self.assertIn("@nome", d)


if __name__ == "__main__":
    unittest.main()
