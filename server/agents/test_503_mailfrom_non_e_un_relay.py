"""`mailfrom:` non è un relay, e il mandato del messaggero non deve prometterlo
(clodia-platform#503).

Il difetto era **testuale**. Il prompt diceva che un mittente Telegram
autorizzato è «un ingress di quello scope, **esattamente come un mittente
email** o una cartella Drive», e le righe subito sotto descrivono un relay che
rifiuta l'handle non in lista e porta il messaggio nel topic. Lette insieme, le
due frasi insegnano che scrivere `mailfrom:<indirizzo>` negli ingress fa
arrivare lì la posta di quel mittente — ed è così che l'agente lo spiegava agli
utenti. Nessun inoltro del genere esiste: la posta si va a leggere, e
`mailfrom:` decide solo se il contenuto è fidato (taint), tranne nei canali a
ingresso stretto introdotti dalla stessa issue (clodia-tools 2.80.0).

Perché il controllo vive sul TESTO. Qui non c'è una config da verificare: la
cosa che ha prodotto la promessa sbagliata è una frase, e se la frase torna non
fallisce nient'altro. Stessa ragione, e stesso idioma, di
`test_messaggero_read_is_not_reply` (#415).

Perché le asserzioni guardano il CAPITOLO delle caselle email e non il file
intero: la spiegazione serve nel punto in cui il modello sta parlando di posta.
Nel capitolo Telegram resterebbe scritta e inutile — e un `assertIn` sul file
intero sarebbe verde anche con la sezione nel posto sbagliato, cioè misurerebbe
qualcosa che non è il difetto.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from ..config import workspace_path

PROMPT = Path(workspace_path(
    "catalogs/packs/base-pack/agents/messaggero/system-prompt.md"))

#: Titolo della sezione introdotta da #503, in un posto solo: se un giorno viene
#: riformulato, il rosso arriva da qui e non da cinque assertIn sparsi.
TITOLO = "### La posta NON funziona come Telegram: `mailfrom:` non è un relay"

#: L'equivalenza che ha prodotto la promessa. Frammento, non frase intera: è la
#: parte che fa il danno, e cercarla tutta renderebbe il controllo aggirabile
#: con una virgola.
EQUIVALENZA = "esattamente come un mittente email"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.prompt = PROMPT.read_text(encoding="utf-8")

    def sezione(self) -> str:
        """Il testo della sola sezione introdotta da #503."""
        self.assertIn(TITOLO, self.prompt, "sezione di #503 sparita dal mandato")
        return self.prompt.split(TITOLO, 1)[1].split("\n## ", 1)[0]

    def capitolo(self, titolo: str) -> str:
        """Il testo di UN capitolo `## ...`, fino al successivo."""
        pezzi = self.prompt.split(f"\n## {titolo}", 1)
        self.assertEqual(2, len(pezzi), f"capitolo «{titolo}» sparito dal mandato")
        return pezzi[1].split("\n## ", 1)[0]


class LaPostaNonEUnRelayTests(Base):
    def test_il_mandato_non_equipara_piu_la_posta_a_telegram(self) -> None:
        """L'equivalenza è il difetto: Telegram ha un relay che filtra, la posta
        no, e metterle sulla stessa riga insegna un filtro che non esiste."""
        self.assertNotIn(EQUIVALENZA, self.prompt)

    def test_la_spiegazione_sta_nel_capitolo_delle_caselle_email(self) -> None:
        self.assertIn(TITOLO, self.capitolo("Caselle email (tool `email.*`)"))

    def test_dice_che_nessuno_inoltra_la_posta_in_un_canale(self) -> None:
        """La frase che toglie la promessa, non una perifrasi: è quella che
        l'agente ripete agli utenti."""
        self.assertIn("nessuno inoltra le mail in un canale", self.prompt)

    def test_distingue_le_due_voci_invece_di_nominarle_insieme(self) -> None:
        """`inbox:` = quale casella (limite d'accesso), `mailfrom:` = di chi ci
        si fida (taint). Nominarle senza distinguerle è il modo in cui la
        seconda viene letta come la prima."""
        # A capo collassati: il mandato è testo a 80 colonne, e una frase
        # spezzata dall'a-capo è la stessa frase.
        sezione = " ".join(self.sezione().split())
        self.assertIn("`inbox:<casella>`", sezione)
        self.assertIn("`mailfrom:<indirizzo>`", sezione)
        # E dice cosa comporta davvero il secondo: si legge lo stesso, marcato.
        self.assertIn("si legge lo stesso", sezione)
        self.assertIn("non fidato", sezione)

    def test_nomina_l_eccezione_dei_canali_stretti(self) -> None:
        """Senza l'eccezione il mandato tornerebbe falso dall'altro lato: nei
        canali a ingresso stretto `mailfrom:` filtra davvero, e un agente che
        ha letto «serve solo al taint» chiamerebbe guasto un rifiuto."""
        self.assertIn("ingresso stretto", self.sezione())

    def test_il_capitolo_telegram_rimanda_invece_di_generalizzare(self) -> None:
        """Il relay Telegram esiste e va descritto; quello che non deve fare è
        parlare a nome della posta."""
        telegram = self.capitolo("Canale Telegram (tool `telegram.*`)")
        self.assertIn("solo per Telegram", telegram)
        self.assertNotIn("mittente email", telegram)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
