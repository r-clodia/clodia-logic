"""Testo di sistema RIPORTATO non convoca nessuno (clodia-platform#480).

Il difetto: incollare un messaggio precedente che contiene `@nome` senza
prefissare ogni riga con `>` produceva menzioni vive. Nell'incidente del
1 ott 2026 un messaggio con UNA convocazione (`@sysadmin`) seguita dal testo
incollato di un dialogo di routing ne ha prodotte TRE, e il router ha aperto
una disambiguazione a tre vie invece di instradare.

L'invariante, detta a voce alta: **una `@` conta per il messaggio in cui è
stata scritta, non per quello in cui è stata riportata.** Il prefisso `>`
resta la forma canonica della citazione; i marcatori coprono il caso — che è
la norma quando si segnala un bug — in cui chi incolla non lo mette.

Il taglio è di RIGA e non di messaggio: il testo che l'autore scrive DOPO il
blocco incollato è suo, e le sue menzioni restano vive. È la differenza fra
una cintura e una museruola, e il test `test_il_testo_dopo_il_riporto_resta_vivo`
è quello che la misura.

Questo file vive in due copie (issue#255), come `mentions.py` che esercita:
    clodia-tools/server/topics/test_480_testo_riportato.py
    clodia-logic/server/api/test_480_testo_riportato.py

Da questo lato c'è in più `RouterTests`: è il router che decide se un turno
parte, e il difetto si vedeva lì — `_tags` tornava tre nomi e il dispatcher
apriva il dialogo. `test_mention_boundary` passa già tutta `GOLDEN_CASES` su
`_tags`/`_tagged`, quindi i casi nuovi sono coperti anche lì; questa classe
tiene la repro della issue in forma leggibile, accanto alla sua diagnosi.
"""
from __future__ import annotations

import unittest

from . import channels, mentions
from .mentions import extract_mentions, extract_tags

#: La repro minima della issue, parola per parola.
REPRO_480 = (
    "@sysadmin ciao, apri una issue: ℹ Sistema\n"
    "Routing: scegli @clodia o @clodia-354."
)


class TestoRiportatoTests(unittest.TestCase):
    def test_la_repro_della_issue_produce_una_sola_menzione(self) -> None:
        self.assertEqual(extract_mentions(REPRO_480), ["sysadmin"])

    def test_la_repro_vale_anche_sui_tag_che_aprono_il_turno(self) -> None:
        # È `extract_tags` che il router interroga: se le due divergessero,
        # il badge e il turno racconterebbero due storie diverse (#255).
        self.assertEqual(extract_tags(REPRO_480), ["sysadmin"])

    def test_il_dialogo_di_routing_incollato_non_convoca_nessuno(self) -> None:
        self.assertEqual(
            extract_mentions("Routing: scegli @clodia o @clodia-354."), [])

    def test_il_resoconto_di_fine_turno_incollato_non_convoca_nessuno(self) -> None:
        self.assertEqual(
            extract_mentions(
                "[turno concluso] @clodia-405 ha terminato il compito."), [])

    def test_l_annuncio_di_turno_fallito_incollato_non_convoca_nessuno(self) -> None:
        self.assertEqual(
            extract_mentions(
                "⚠️ Il turno di **@fullstack-dev-271** è terminato con un errore."),
            [])

    def test_le_pill_incollate_non_convocano_nessuno(self) -> None:
        self.assertEqual(
            extract_mentions("<!-- choices=@clodia,@mario -->"), [])

    def test_una_convocazione_davanti_al_riporto_sopravvive(self) -> None:
        testo = ("@clodia guarda cosa è successo:\n"
                 "[turno concluso] @sysadmin ha terminato il compito.")
        self.assertEqual(extract_mentions(testo), ["clodia"])

    def test_il_testo_dopo_il_riporto_resta_vivo(self) -> None:
        # Il taglio arriva a fine RIGA, non a fine messaggio: quello che
        # l'autore scrive dopo è farina sua e convoca davvero.
        testo = ("Routing: scegli @clodia o @mario.\n"
                 "vabbè, fai tu @sysadmin")
        self.assertEqual(extract_mentions(testo), ["sysadmin"])

    def test_il_marcatore_non_azzera_quello_che_lo_precede_sulla_riga(self) -> None:
        # `ℹ Sistema` è l'intestazione della bolla copiata dalla webui e
        # chiude la riga: la convocazione che la precede è dell'autore.
        self.assertEqual(
            extract_mentions("@sysadmin apri una issue: ℹ Sistema"), ["sysadmin"])


class NonRegressioneTests(unittest.TestCase):
    """Ciò che deve continuare a convocare. Senza questi, la cintura di #480
    sarebbe indistinguibile da un parser che ha semplicemente smesso di
    leggere le menzioni."""

    def test_due_convocazioni_vere_restano_due(self) -> None:
        # È il caso che DEVE ancora aprire il dialogo di routing: due `@`
        # scritte apposta non sono un testo incollato.
        self.assertEqual(
            extract_mentions("@clodia e @sysadmin, chi prende la issue?"),
            ["clodia", "sysadmin"])

    def test_una_parola_qualunque_non_e_un_marcatore(self) -> None:
        # I marcatori sono stringhe che la PIATTAFORMA emette. Se bastasse una
        # parola comune, chiunque potrebbe rendere muta una menzione vera
        # scrivendola prima del nome.
        self.assertEqual(extract_mentions("sistema a posto, @clodia vai"),
                         ["clodia"])
        self.assertEqual(extract_mentions("routing lento, @clodia guarda"),
                         ["clodia"])

    def test_la_menzione_dentro_una_riga_citata_resta_esclusa(self) -> None:
        # La regola vecchia non è stata sostituita, solo affiancata.
        self.assertEqual(
            extract_mentions("> @clodia diceva\nrispondo io: @luca"), ["luca"])


class ContrattoCondivisoTests(unittest.TestCase):
    """Idioma #457: un comportamento nuovo del parser entra in `GOLDEN_CASES`,
    la tabella che viaggia dentro il modulo perché la suite di ENTRAMBI i
    repository la esegua sui propri entry point (#255). Senza questa
    asserzione una delle due copie potrebbe cambiare da sola."""

    CASI_480 = (
        (REPRO_480, ["sysadmin"]),
        ("Routing: scegli @clodia o @clodia-354.", []),
        ("[turno concluso] @clodia-405 ha terminato il compito.", []),
        ("Routing: scegli @clodia o @mario.\nvabbè, fai tu @sysadmin",
         ["sysadmin"]),
        ("sistema a posto, @clodia vai", ["clodia"]),
    )

    def test_i_casi_di_480_sono_nel_golden_condiviso(self) -> None:
        for caso in self.CASI_480:
            with self.subTest(testo=caso[0]):
                self.assertIn(caso, mentions.GOLDEN_CASES)


class RouterTests(unittest.TestCase):
    """Gli entry point che decidono se un turno parte."""

    def test_il_router_legge_una_sola_menzione_nella_repro(self) -> None:
        # Era qui che si rompeva: con tre nomi il dispatcher prende il ramo
        # `len(hard_targets) >= 2` e apre la disambiguazione invece di
        # instradare a chi era stato taggato in chiaro.
        self.assertEqual(channels._tags(REPRO_480), ["sysadmin"])

    def test_il_destinatario_risolto_e_quello_scritto_in_chiaro(self) -> None:
        self.assertEqual(channels._tagged(REPRO_480), "sysadmin")

    def test_due_convocazioni_vere_arrivano_ancora_in_due_al_router(self) -> None:
        # Il dialogo di disambiguazione deve continuare a esistere: serve a
        # chi davvero ne ha taggati due.
        self.assertEqual(channels._tags("@clodia e @sysadmin, chi prende?"),
                         ["clodia", "sysadmin"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
