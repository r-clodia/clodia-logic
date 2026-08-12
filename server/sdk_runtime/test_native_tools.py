"""Gli strumenti nativi diventano una dichiarazione del seed.

Il piano scritto in `agents-notebook` A9 diceva di passare `allowed_tools`.
**Misurato il 12 ago 2026, dentro il container, non dedotto:**

    claude -p … --allowed-tools WebFetch      → l'agente USA WebSearch comunque
    claude -p … --disallowed-tools WebSearch  → «WebSearch non è disponibile
                                                 in questo ambiente»

`allowed_tools` è una lista di permessi, non un filtro dell'insieme disponibile.
Il seed dichiara comunque un'allowlist — è la forma leggibile — e la sottrazione
si calcola qui.

E la ragione per cui serve, misurata nello stesso container col proxy attivo e
`curl example.com` che non passa:

    WebFetch  → example.com                BLOCCATO ("Socket is closed")
    WebFetch  → raw.githubusercontent.com  OK
    WebSearch → query qualunque            OK, 6 risultati

`WebFetch` lo esegue la CLI e il proxy lo arbitra; `WebSearch` lo esegue il
provider (`type:"web_search_20250305"` nel bundle) e nessuna policy di rete lo
vede. Contro quello, l'unico strato è la configurazione del runtime.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import native_tools as nt


class TheKnownSetComesFromTheCliTests(unittest.TestCase):
    def test_the_fallback_is_a_photograph_of_the_declaration(self):
        """Se il `.d.ts` è leggibile, il ripiego deve combaciare. Un ripiego che
        divergesse in silenzio negherebbe nomi inesistenti e lascerebbe passare
        quelli nuovi — cioè esattamente i due errori che deve evitare."""
        if not nt.SDK_TYPES.is_file():
            self.skipTest("sdk-tools.d.ts assente (sviluppo fuori dal container)")
        dal_file = nt.known_tools()
        ripiego = {nt.ALIAS.get(n, n) for n in nt.KNOWN_FALLBACK}
        self.assertEqual(dal_file, ripiego,
                         "la CLI dichiara un insieme diverso dal ripiego: "
                         "un aggiornamento ha aggiunto o togliuto uno strumento")

    def test_the_file_tools_carry_the_name_the_agent_sees(self):
        """Nel `.d.ts` sono `FileRead`/`FileWrite`/`FileEdit`; all'agente
        arrivano come `Read`/`Write`/`Edit`, e `disallowed_tools` vuole quelli."""
        noti = nt.known_tools()
        for n in ("Read", "Write", "Edit"):
            self.assertIn(n, noti)
        for n in ("FileRead", "FileWrite", "FileEdit"):
            self.assertNotIn(n, noti)

    def test_the_web_tools_are_in_the_known_set(self):
        noti = nt.known_tools()
        self.assertIn("WebSearch", noti)
        self.assertIn("WebFetch", noti)


class TheSubtractionTests(unittest.TestCase):
    def test_nobody_declaring_restricts_nothing(self):
        """`None` = non mi pronuncio. Una lista vuota che chiudesse tutto
        verrebbe spenta il giorno dopo, e allora non esisterebbe."""
        self.assertEqual(nt.disallowed_for(None), [])

    def test_an_empty_declaration_denies_everything(self):
        """`[]` invece è una dichiarazione, e va rispettata."""
        negati = nt.disallowed_for([])
        self.assertEqual(set(negati), nt.known_tools())

    def test_what_is_declared_survives_and_the_rest_goes(self):
        negati = nt.disallowed_for(["Read", "Write", "Skill"])
        for concesso in ("Read", "Write", "Skill"):
            self.assertNotIn(concesso, negati)
        for tolto in ("WebSearch", "WebFetch", "Bash", "Agent", "CronCreate"):
            self.assertIn(tolto, negati)

    def test_a_family_wildcard_grants_the_family(self):
        """`Task*` concede i sei verbi dei task. Elencarli a uno a uno vuol dire
        dichiarare sei righe dove la decisione è una — e alla prossima aggiunta
        averne cinque su sei."""
        negati = nt.disallowed_for(["Task*"])
        self.assertFalse([n for n in negati if n.startswith("Task")])
        self.assertIn("WebSearch", negati)

    def test_a_lone_star_is_not_a_wildcard_that_opens_everything(self):
        """Un `*` da solo diventerebbe un prefisso vuoto, e `startswith("")` è
        vero per tutto: concederebbe l'intero insieme per una riga distratta."""
        negati = nt.disallowed_for(["*"])
        self.assertIn("WebSearch", negati)
        self.assertIn("Bash", negati)

    def test_a_bash_pattern_grants_bash(self):
        """`Bash(git:*)` concede `Bash`: negarlo per intero cancellerebbe il
        ritaglio invece di applicarlo. Il taglio fine sta nei pattern, che è dove
        la CLI lo sa fare."""
        self.assertNotIn("Bash", nt.disallowed_for(["Bash(git:*)"]))


class TheFloorAndTheSeedUnionTests(unittest.TestCase):
    """Il pavimento dell'arciseed si SOMMA a quello del seed: un seed che
    dichiara il proprio mestiere non deve per questo perdere `Read`."""

    def _resolve(self, proprio, pavimento):
        from types import SimpleNamespace
        from . import session as S
        from ..agents import loader

        def get(nome):
            if nome == "archseed":
                return SimpleNamespace(native_tools=pavimento)
            return SimpleNamespace(native_tools=proprio)

        with patch.object(loader.registry, "get_by_name", get):
            return S._resolve_native_allowed("qualcuno")

    def test_the_seed_adds_to_the_floor(self):
        fuori = self._resolve(["WebFetch"], ["Read", "Write"])
        self.assertEqual(fuori, ["Read", "WebFetch", "Write"])

    def test_a_seed_that_says_nothing_gets_the_floor(self):
        self.assertEqual(self._resolve(None, ["Read"]), ["Read"])

    def test_nobody_declaring_anywhere_is_no_restriction(self):
        self.assertIsNone(self._resolve(None, None))


class TheGatewayVerbsSurviveTests(unittest.TestCase):
    """Trappola 1 di A9. Il gateway è montato come server MCP e i suoi ~90 verbi
    arrivano al modello come `mcp__clodia-tools__*`. Negare per nome i soli tool
    nativi non li tocca — e questo test è qui perché la prima stesura del piano
    voleva passare un `allowed_tools`, che li avrebbe tagliati tutti in un colpo.
    """

    def test_no_mcp_namespace_ends_up_in_the_denied_list(self):
        negati = nt.disallowed_for(["Read"])
        self.assertFalse([n for n in negati if n.startswith("mcp__")])

    def test_the_known_set_contains_no_gateway_verb(self):
        noti = nt.known_tools()
        self.assertFalse([n for n in noti if "." in n or n.startswith("mcp__")])


if __name__ == "__main__":
    unittest.main()
