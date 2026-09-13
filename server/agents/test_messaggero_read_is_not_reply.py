"""Un ordine di leggere non è un mandato a spedire (clodia-logic#415).

L'11 set 2026, canale SEAL-2 `titul-brightnode`: a `messaggero` è stato chiesto
di **leggere** un'email che sollecitava conferme contrattuali. Ha risposto al
mittente impegnando lo studio, senza che il testo fosse mai stato mostrato né
approvato. L'unico controllo scattato è stato quello sulla **destinazione**
(`mailto:`), che non guarda il corpo del messaggio.

Perché il controllo vive qui, sul TESTO del mandato, e non su una config.
`catalogs/packs/base-pack/agents/messaggero/agent.yaml` dichiara
`gated_tools: []` e `gated_in_channel: []` **vuoti di proposito** — decisione
dell'owner del 7 ago 2026, motivata per venti righe nello stesso file: il
presidio sta sulla destinazione «finché la catena `origin` non è in
enforcement». Finché quella scelta regge, sul CONTENUTO non esiste nessuna
barriera oltre le parole del system prompt: se sparissero, non fallirebbe
nient'altro.

Le asserzioni guardano la **sezione** «Policy outbound», non il file intero, per
la stessa ragione già scritta in `test_seed_mandates`: `verbatim` nel file c'era
già — nella sezione Telegram, cioè dove l'incidente non è successo. Un `assertIn`
sul file intero sarebbe stato **verde prima del fix**, cioè avrebbe misurato il
punto sbagliato senza mai vedere il difetto.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from ..config import workspace_path

PROMPT = Path(workspace_path(
    "catalogs/packs/base-pack/agents/messaggero/system-prompt.md"))

#: Il titolo della sottosezione introdotta da #415. Costante e non letterale
#: ripetuto: se un giorno viene riformulato, il rosso arriva da un punto solo.
TITOLO = "### Leggere non è rispondere"


class LeggereNonERispondereTests(unittest.TestCase):

    def setUp(self) -> None:
        self.prompt = PROMPT.read_text(encoding="utf-8")

    def _policy_outbound(self) -> str:
        """Il testo della sola sezione che governa l'invio verso l'esterno."""
        dopo = self.prompt.split("## Policy outbound", 1)
        self.assertEqual(2, len(dopo),
                         "sezione «Policy outbound» sparita dal mandato del "
                         "messaggero: è lì che vive l'unico vincolo sul "
                         "contenuto di ciò che spedisce")
        return dopo[1].split("\n## ", 1)[0]

    def test_la_disambiguazione_sta_nella_policy_outbound(self) -> None:
        """E non altrove nel file.

        Spostata sotto «Canale Telegram» resterebbe scritta e sarebbe inutile:
        l'incidente è su email, e il modello legge la policy outbound nel
        momento in cui sta per spedire.
        """
        self.assertIn(TITOLO, self._policy_outbound())

    def test_la_disambiguazione_e_scritta_una_volta_sola(self) -> None:
        """Due copie divergono alla prima modifica, e l'agente ne legge una."""
        self.assertEqual(1, self.prompt.count(TITOLO))

    def test_un_ordine_di_leggere_non_autorizza_a_rispondere(self) -> None:
        """Il difetto in una riga: «leggi» trattato come «rispondi».

        Il principio generale («non inviare senza mandato esplicito») c'era già
        ed è stato letto come soddisfatto — un ordine c'era, era quello di
        leggere. Serve che i due atti siano nominati come distinti.
        """
        sezione = self._policy_outbound()
        self.assertIn("NON autorizza a", sezione)
        self.assertIn("rispondere", sezione)

    def test_il_testo_spedito_va_approvato_prima_e_verbatim(self) -> None:
        """Il vincolo esisteva solo per Telegram: ora vale per ogni invio.

        «ho risposto a nome tuo» detto dopo non è un'approvazione: è un fatto
        compiuto. L'approvazione è sul testo, prima.
        """
        sezione = self._policy_outbound()
        self.assertIn("verbatim", sezione)
        self.assertIn("PRIMA di spedirlo", sezione)

    def test_il_gate_sulla_destinazione_non_e_un_gate_sul_contenuto(self) -> None:
        """Il pezzo che manca a un `gated_tools` vuoto.

        Approvare `mailto:ezio@…` autorizza l'indirizzo, non il testo che ci si
        mette dentro: trattarla come copertura del corpo è la delega in bianco
        descritta nell'incidente.
        """
        sezione = self._policy_outbound()
        self.assertIn("gate sulla destinazione", sezione)
        self.assertIn("non è un gate sul contenuto", sezione)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
