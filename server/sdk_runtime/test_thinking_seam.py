"""Il ragionamento in streaming non deve saldarsi riga su riga.

Il difetto segnalato il 3 set 2026: «nella thinking box le righe si sovrappongono
e diventa illeggibile». Il CSS non c'entra — `.live-think` ha `white-space:
pre-wrap` e `line-height: 1.5`, e reso con quelle regole il riquadro è
perfettamente leggibile. Il guasto è a monte, in ciò che il produttore pubblica.

Il testo visibile ha `_BlockFilter`, che fra due blocchi distinti inserisce
`\\n\\n`, e la sua docstring dice il perché: **il confine fra blocchi lo conosce
solo il produttore**, e ricostruirlo nel chiamante vuol dire tenerne due copie.
Il ragionamento non aveva niente: `thinking_delta` veniva pubblicato grezzo e la
webui fa `think = think + delta`.

Finché il turno è un blocco solo non si vede. Ma un turno ne ha molti — col
thinking adattivo il ragionamento si riapre dopo ogni tool-call — e l'ultimo
delta di un blocco non finisce con un capo a riga: l'ultima riga del blocco N si
salda alla prima del blocco N+1 sulla stessa riga. Nei runtime codex/opencode è
peggio, perché lì un item `reasoning` è un paragrafo intero.

Questi test guardano la cucitura, non i pixel: è l'unica cosa che il server
possa garantire, e il difetto visivo è la sua conseguenza diretta.
"""
from __future__ import annotations

import unittest

from .session import _BlockFilter, _ThinkSeam


class LaCucituraDelRagionamento(unittest.TestCase):
    def test_due_blocchi_non_si_saldano(self) -> None:
        """IL CASO SEGNALATO: senza confine, «…canone precedente.Devo
        controllare» finisce tutto su una riga."""
        s = _ThinkSeam()
        out = s.feed(0, "Il canone era 1.000 EUR.")
        out += s.feed(1, "Devo controllare l'art. 12.")
        self.assertIn("\n\n", out)
        self.assertNotIn("EUR.Devo", out)

    def test_i_delta_dello_stesso_blocco_non_vengono_spezzati(self) -> None:
        """Il confine è il BLOCCO, non il delta: altrimenti ogni token andrebbe
        a capo e il ragionamento diventerebbe una colonna di parole."""
        s = _ThinkSeam()
        out = "".join(s.feed(0, p) for p in ("Il", " canone", " era", " 1.000."))
        self.assertEqual("Il canone era 1.000.", out)

    def test_nessun_capo_a_vuoto_se_il_testo_lo_ha_gia(self) -> None:
        """Fra due blocchi già spaziati non si aggiunge niente, o compaiono
        righe vuote a vuoto e il riquadro si allunga senza contenuto."""
        s = _ThinkSeam()
        out = s.feed(0, "Primo blocco.\n\n")
        out += s.feed(1, "Secondo blocco.")
        self.assertNotIn("\n\n\n", out)

    def test_il_primo_blocco_non_comincia_con_un_capo(self) -> None:
        s = _ThinkSeam()
        self.assertFalse(s.feed(0, "Comincio a ragionare.").startswith("\n"))

    def test_il_delta_vuoto_non_conta_come_blocco(self) -> None:
        """Un delta vuoto non deve consumare il confine: altrimenti il blocco
        successivo lo troverebbe già speso e si salderebbe comunque."""
        s = _ThinkSeam()
        out = s.feed(0, "Primo.")
        self.assertEqual("", s.feed(1, ""))
        out += s.feed(1, "Secondo.")
        self.assertIn("\n\n", out)

    def test_molti_blocchi_come_in_un_turno_con_tool(self) -> None:
        """Il caso reale: il ragionamento si riapre dopo ogni tool-call."""
        s = _ThinkSeam()
        testo = "".join(s.feed(i, f"Passo {i}: verifico.") for i in range(5))
        self.assertEqual(5, len(testo.split("\n\n")))


class RagionamentoInteroPerBlocco(unittest.TestCase):
    """codex e opencode consegnano paragrafi interi, non delta.

    Là il contatore è l'indice: ogni item `reasoning` è un blocco nuovo. Senza
    cucitura due paragrafi finivano attaccati senza nemmeno uno spazio.
    """

    def test_due_paragrafi_restano_due_paragrafi(self) -> None:
        s, n = _ThinkSeam(), 0
        out = ""
        for par in ("Leggo il contratto e annoto le clausole economiche.",
                    "Poi confronto con la proposta della controparte."):
            n += 1
            out += s.feed(n, par)
        self.assertIn("clausole economiche.\n\nPoi confronto", out)


class ComeIlTestoVisibile(unittest.TestCase):
    """La stessa proprietà che `_BlockFilter` garantisce già per le bolle.

    Se un domani il testo visibile perdesse il separatore, questo test cade
    insieme all'altro: sono la stessa regola su due canali.
    """

    def test_entrambi_i_canali_separano_i_blocchi(self) -> None:
        f = _BlockFilter()
        f.feed(0, "primo")
        f.end_block()
        visibile = f.feed(1, "secondo")
        s = _ThinkSeam()
        s.feed(0, "primo")
        pensato = s.feed(1, "secondo")
        self.assertTrue(visibile.startswith("\n\n"), "il testo visibile perde il confine")
        self.assertTrue(pensato.startswith("\n\n"), "il ragionamento perde il confine")


class IPuntiDiEmissioneLaUsano(unittest.TestCase):
    """Una classe corretta che nessuno chiama non ripara niente.

    I tre punti che pubblicano `thinking_chunk` sono in tre runtime diversi
    (SDK Claude, codex, opencode) e si sono già scordati questa cosa una volta:
    il ramo `text_delta` aveva il filtro, quello `thinking_delta` accanto no.
    """

    def test_nessun_thinking_chunk_pubblica_il_delta_grezzo(self) -> None:
        from pathlib import Path
        src = (Path(__file__).parent / "session.py").read_text()
        grezzi = []
        for i, riga in enumerate(src.splitlines(), 1):
            if '"delta": delta["thinking"]' in riga or '"delta": p["text"]' in riga \
                    or ('"delta": text' in riga and "reasoning" not in riga):
                # è dentro un payload di thinking_chunk?
                intorno = "\n".join(src.splitlines()[max(0, i - 6):i])
                if "thinking_chunk" in intorno:
                    grezzi.append(i)
        self.assertEqual([], grezzi,
                         f"thinking_chunk pubblicato senza cucitura alle righe {grezzi}")

    def test_tutti_e_tre_i_runtime_cuciono(self) -> None:
        from pathlib import Path
        src = (Path(__file__).parent / "session.py").read_text()
        self.assertEqual(3, src.count("seam.feed("),
                         "i punti che pubblicano thinking_chunk sono tre: "
                         "SDK Claude, codex, opencode")


if __name__ == "__main__":
    unittest.main()
