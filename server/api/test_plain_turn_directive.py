"""Un turno assegnato dal router deve dire all'agente che il turno è suo.

Segnalato il 5 set 2026: «quando non menziono nessuno, il router seleziona un
agent ma poi questo non esegue la richiesta e dice che resta in attesa di
istruzioni».

Il percorso è questo. Nessuna menzione → `_post_message` costruisce il piano →
`turn_kind = "plain"` → `_start_turn` chiama `_tag_directive("plain", …)`, che
ritornava **None**. Con `None` il prompt del primo turno è la sola storia del
canale (`base + ("" if not directive)`), e su un turno riusato è il messaggio
nudo. In nessuno dei due casi c'è una riga che dica all'agente che tocca a lui.

Chi legge una conversazione in cui nessuno lo nomina non ha motivo di concludere
che debba agire: «resto in attesa di istruzioni» è la lettura corretta di un
prompt che non contiene un mandato.

E `plain` è **il percorso più comune** — quello che si imbocca quando l'utente
scrive normalmente. Tutti gli altri il mandato ce l'hanno da sempre: `direct`
(menzione), `routed` (multi-intent), `coordinamento`, `topic-bootstrap`,
`disambigua`. Mancava solo questo, ed è il difetto: non una direttiva sbagliata,
una direttiva assente.
"""
from __future__ import annotations

import unittest

from .channels import _tag_directive

_TESTO = "Puoi verificare la scadenza del contratto CPTO?"


class IlMandatoCiDeveEssere(unittest.TestCase):

    def test_plain_non_e_piu_muto(self) -> None:
        """IL CASO SEGNALATO: era `None`, cioè nessuna istruzione."""
        d = _tag_directive("plain", "davide", _TESTO)
        self.assertIsNotNone(d, "un turno senza mandato è un agente in attesa")
        self.assertIn(_TESTO, d)

    def test_dice_che_il_turno_e_suo_e_che_nessun_altro_risponde(self) -> None:
        """Le due cose che l'agente non poteva sapere: di essere stato scelto, e
        di essere il solo. Senza la seconda, «forse tocca a un collega» resta una
        lettura ragionevole del silenzio."""
        d = _tag_directive("plain", "davide", _TESTO).lower()
        self.assertIn("turno è tuo", d)
        self.assertIn("nessun altro", d)

    def test_nomina_chi_ha_scritto(self) -> None:
        self.assertIn("davide", _tag_directive("plain", "davide", _TESTO))

    def test_lascia_una_via_duscita_se_il_router_ha_sbagliato(self) -> None:
        """Senza, l'unico modo di obbedire sarebbe rispondere fuori dominio — e
        una risposta inventata è peggio del silenzio che stiamo togliendo."""
        d = _tag_directive("plain", "davide", _TESTO).lower()
        self.assertIn("non è del tuo dominio", d)
        self.assertIn("@nome", d)

    def test_dice_esplicitamente_di_non_restare_in_attesa(self) -> None:
        """È la frase che l'agente produceva: vale nominarla."""
        self.assertIn("attesa di istruzioni",
                      _tag_directive("plain", "davide", _TESTO).lower())


class GliAltriPercorsiRestanoComeErano(unittest.TestCase):
    """La correzione aggiunge un ramo, non ne cambia altri."""

    def test_direct_invariato(self) -> None:
        d = _tag_directive("direct", "davide", _TESTO)
        self.assertIn("[RICHIESTA DIRETTA]", d)

    def test_routed_invariato(self) -> None:
        self.assertIn("[ROUTING AUTOMATICO]", _tag_directive("routed", "davide", _TESTO))

    def test_coordinamento_invariato(self) -> None:
        self.assertIn("[COORDINAMENTO]", _tag_directive("coordinamento", "davide", _TESTO))

    def test_un_kind_sconosciuto_resta_senza_direttiva(self) -> None:
        """`None` è ancora il default: il fallback del chiamante esiste per
        quello, e riempirlo per tutti nasconderebbe un kind scritto male."""
        self.assertIsNone(_tag_directive("kind-che-non-esiste", "davide", _TESTO))


class NessunPercorsoDiTurnoRestaMuto(unittest.TestCase):
    """Guard: i `turn_kind` che il codice può produrre devono avere un mandato.

    Il difetto non è nato da una direttiva sbagliata ma da una MANCANTE, e un
    kind nuovo aggiunto domani cadrebbe nello stesso modo — silenziosamente,
    perché `None` è un valore legittimo per il chiamante.
    """

    #: I valori che `_post_message` e i rami di delega passano come `kind` a
    #: `_start_turn`. `disambigua` e `debug` sono esclusi: il primo ha la sua
    #: direttiva, il secondo passa il testo così com'è di proposito.
    KIND_DI_TURNO = ("plain", "routed", "coordinamento", "direct", "topic-bootstrap")

    def test_ognuno_porta_un_mandato(self) -> None:
        muti = [k for k in self.KIND_DI_TURNO
                if not _tag_directive(k, "davide", _TESTO)]
        self.assertEqual([], muti,
                         f"turni senza mandato: {muti} — l'agente non sa che tocca a lui")

    def test_ognuno_porta_anche_il_messaggio(self) -> None:
        """Un mandato senza il testo manderebbe l'agente a cercarlo nella storia."""
        senza = [k for k in self.KIND_DI_TURNO
                 if _TESTO not in (_tag_directive(k, "davide", _TESTO) or "")]
        self.assertEqual([], senza, f"mandato senza il messaggio: {senza}")


if __name__ == "__main__":
    unittest.main()
