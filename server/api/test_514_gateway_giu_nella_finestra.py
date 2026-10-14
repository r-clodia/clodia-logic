"""clodia-platform#514 · «modello lento» o «gateway bloccato»? I guasti del
gateway vanno DATATI, se no la domanda non ha risposta.

L'incidente, nella forma che si ripete: un turno opencode consuma tutto il suo
budget e muore senza output. Dai log si vede che in quella stessa finestra
`clodia-tools:7849` andava in `Read timed out`/502 — ma lo si vede *dopo*, a
mano, incrociando due file; chi legge l'errore nella stanza non ha niente, e
l'unica frase disponibile parla del modello.

Il breaker di `GatewayHTTP` sapeva già tutto ciò che serve: conta i fallimenti
di connessione e a soglia smette di provare. Quello che NON teneva è il
*quando* — e «quando» è l'unica cosa che permette di dire se il guasto cade
dentro il turno scaduto o è di tre ore prima. Qui si verifica che il registro
dati i guasti, che la finestra sia davvero una finestra, e che la frase dica una
concomitanza con i suoi numeri invece di nominare un colpevole.

Limite strutturale, dichiarato e non aggirabile da questo lato: qui passano solo
le chiamate che *l'agent-server* fa al gateway — i due client `topics` e `pdp`.
Le tool call MCP che opencode manda a `clodia-tools` per conto suo non passano
di qui, e nemmeno il mint PKI o `api.observe`, che hanno il loro trasporto.
L'assenza di guasti non è quindi la prova che il gateway stesse bene: è per
questo che la nota tace invece di scrivere «allora è il modello».
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

import requests

from server.api import gateway_http as G


class _Resp:
    status_code = 200


class _Resp500:
    status_code = 500
    text = "boom"


class _RegistroBase(unittest.TestCase):
    def setUp(self) -> None:
        G.reset_guasti()
        self.addCleanup(G.reset_guasti)


class IGuastiSonoDatatiTests(_RegistroBase):

    def test_senza_guasti_la_nota_tace(self) -> None:
        """Il ramo che deve restare muto: nessun guasto ⇒ nessuna frase. Una
        nota che dicesse «il gateway ha risposto, quindi è il modello» sarebbe
        di nuovo una diagnosi al posto di un'osservazione — e con un breaker che
        vede solo due client sarebbe anche falsa."""
        self.assertEqual("", G.nota_gateway(time.monotonic() - 60))

    def test_un_fallimento_di_connessione_entra_nella_nota(self) -> None:
        """Rosso prima: il guasto esisteva solo come riga di log, fuori dalla
        portata di chi legge l'errore."""
        inizio = time.monotonic()
        http = G.GatewayHTTP("topics", threshold=99, cooldown=60.0)
        with mock.patch.object(requests, "request",
                               side_effect=requests.ConnectionError("read timeout")):
            with self.assertRaises(requests.ConnectionError):
                http.get("http://gw/x")
        nota = G.nota_gateway(inizio)
        self.assertIn("non ha risposto 1 volte", nota)
        self.assertIn("topics", nota)
        self.assertIn("prima", nota, f"la nota non data l'ultimo guasto: {nota}")

    def test_il_conto_e_quello_della_finestra_non_quello_di_sempre(self) -> None:
        """Un gateway caduto e tornato PRIMA che il turno cominciasse non spiega
        quel turno: attribuirglielo manderebbe l'indagine nel posto sbagliato
        (stessa regola di `loop_lag.stall_since`)."""
        http = G.GatewayHTTP("topics", threshold=99, cooldown=60.0)
        with mock.patch.object(requests, "request",
                               side_effect=requests.ConnectionError("vecchio")):
            for _ in range(3):
                with self.assertRaises(requests.ConnectionError):
                    http.get("http://gw/x")
        inizio = time.monotonic()          # ← il turno comincia QUI
        with mock.patch.object(requests, "request",
                               side_effect=requests.ConnectionError("dentro")):
            with self.assertRaises(requests.ConnectionError):
                http.get("http://gw/x")
        self.assertEqual(1, len(G.guasti_da(inizio)),
                         "i guasti precedenti alla finestra sono stati contati")
        self.assertEqual(4, len(G.guasti_da(inizio - 3600)),
                         "allargando la finestra devono tornare tutti")
        self.assertIn("non ha risposto 1 volte", G.nota_gateway(inizio))

    def test_anche_il_rifiuto_a_breaker_aperto_e_una_chiamata_non_servita(self) -> None:
        """Nei minuti peggiori il fail-fast SOSTITUISCE i tentativi: contare solo
        i fallimenti di connessione darebbe il numero più piccolo proprio quando
        il gateway sta peggio."""
        http = G.GatewayHTTP("topics", threshold=2, cooldown=60.0)
        with mock.patch.object(requests, "request",
                               side_effect=requests.ConnectionError("giù")):
            for _ in range(2):
                with self.assertRaises(requests.ConnectionError):
                    http.get("http://gw/x")
            inizio = time.monotonic()
            for _ in range(3):
                with self.assertRaises(G.CircuitOpen):
                    http.get("http://gw/x")
        self.assertEqual(3, len(G.guasti_da(inizio)))

    def test_una_risposta_http_non_e_un_guasto_di_connessione(self) -> None:
        """Il ramo che già funzionava, e che va tenuto: il gateway che risponde
        500 c'è ed è vivo. Se finisse nel registro, ogni turno con un 500 di
        passaggio sembrerebbe un turno passato a parlare col muro."""
        inizio = time.monotonic()
        http = G.GatewayHTTP("topics", threshold=2, cooldown=60.0)
        with mock.patch.object(requests, "request", return_value=_Resp500()):
            for _ in range(4):
                self.assertEqual(500, http.get("http://gw/x").status_code)
        self.assertEqual([], G.guasti_da(inizio))
        self.assertEqual("", G.nota_gateway(inizio))

    def test_la_nota_nomina_i_servizi_coinvolti(self) -> None:
        """`topics` e `pdp` sono due client distinti sullo stesso gateway: quale
        dei due è caduto è il primo indizio di dove guardare."""
        inizio = time.monotonic()
        with mock.patch.object(requests, "request",
                               side_effect=requests.ConnectionError("giù")):
            for nome in ("topics", "pdp", "topics"):
                with self.assertRaises(requests.ConnectionError):
                    G.GatewayHTTP(nome, threshold=99).get("http://gw/x")
        nota = G.nota_gateway(inizio)
        self.assertIn("non ha risposto 3 volte", nota)
        self.assertIn("pdp", nota)
        self.assertIn("topics", nota)

    def test_il_successo_non_cancella_la_storia_della_finestra(self) -> None:
        """Il breaker azzera il suo contatore quando il gateway torna, e deve:
        serve a riprovare. Il registro no — un gateway caduto e tornato DENTRO
        il turno è esattamente il caso che si sta cercando di spiegare."""
        inizio = time.monotonic()
        http = G.GatewayHTTP("topics", threshold=99, cooldown=60.0)
        with mock.patch.object(requests, "request",
                               side_effect=[requests.ConnectionError("giù"), _Resp()]):
            with self.assertRaises(requests.ConnectionError):
                http.get("http://gw/x")
            self.assertEqual(200, http.get("http://gw/x").status_code)
        self.assertEqual(1, len(G.guasti_da(inizio)))

    def test_la_nota_dichiara_una_concomitanza_non_una_causa(self) -> None:
        """La lezione della #358, già pagata una volta: una frase che afferma la
        causa manda a cercare dalla parte sbagliata e nessuno la rimette in
        discussione."""
        inizio = time.monotonic()
        with mock.patch.object(requests, "request",
                               side_effect=requests.ConnectionError("giù")):
            with self.assertRaises(requests.ConnectionError):
                G.GatewayHTTP("topics", threshold=99).get("http://gw/x")
        nota = G.nota_gateway(inizio)
        self.assertIn("nella stessa finestra", nota)
        for parola in ("causa", "colpa", "a causa di", "è stato il gateway"):
            self.assertNotIn(parola, nota)


class IlRegistroNonCresceSenzaFineTests(_RegistroBase):
    """Un registro in memoria senza tetto è una perdita lenta: il tetto è
    dichiarato come SHORTCUT e va verificato, se no resta un'intenzione."""

    def test_tiene_solo_il_passato_recente(self) -> None:
        http = G.GatewayHTTP("topics", threshold=10 ** 6, cooldown=60.0)
        with mock.patch.object(requests, "request",
                               side_effect=requests.ConnectionError("giù")):
            for _ in range(G._GUASTI.maxlen + 50):
                with self.assertRaises(requests.ConnectionError):
                    http.get("http://gw/x")
        self.assertEqual(G._GUASTI.maxlen, len(G._GUASTI))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
