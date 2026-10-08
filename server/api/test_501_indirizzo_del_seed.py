"""Un agente, un indirizzo (issue clodia-platform#501).

Il 3 ott 2026 su `SEAL-1/tomato-blogging` il goal watch ha postato il suo primo
promemoria e **non è partito nessun turno**. Il messaggio, così come è stato
scritto dalla piattaforma, conteneva due destinatari per un agente solo:

    tags: ['clodia', 'clodia-354']

`clodia` non dichiara `multi_spawn`, quindi `clodia-354` non è un'istanza
indirizzabile: è l'etichetta di spawn che si legge in chat, finita nel testo
dell'obiettivo e ricopiata di peso dentro un messaggio di sistema. Due nomi
distinti per il router ⇒ R3 (una menzione per messaggio) ⇒ dialogo di routing o
nessun turno, cioè esattamente ciò che il promemoria esiste per evitare.

Due difetti, e questa suite li misura separati perché si curano in due punti:

1. **la citazione copriva una riga sola** — `f"> {testo}"` su un obiettivo di N
   righe cita la prima e lascia VIVE le `@` delle righe 2..N. È la stessa
   famiglia di #480: una menzione conta per il messaggio in cui è stata
   scritta, non per quello che la riporta;
2. **l'etichetta di spawn usata come indirizzo** — dove la piattaforma compone
   un `@`, il nome va ridotto con la mappa seed/spawn (`indirizzo`), mai per
   confronto fra stringhe (#260, #286, #294).

Più la rete di sicurezza del router, per il testo che hanno già scritto gli
umani e per i messaggi vecchi: `@seed-N` su un seed a istanza unica vale
`@seed`, invece di produrre un secondo destinatario.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channels, goal_watch, mentions, topics
from .test_480_resoconti_senza_sigillo import CHAIN, _Chat, _Spec
from .test_channels import _a


#: Il testo dell'obiettivo come stava nel meta del canale il 3 ott: due righe,
#: e la seconda porta un `@` vivo con l'etichetta di spawn.
GOAL_INCIDENTE = (
    "clodia-354: Davide, i due articoli (…) sono scritti e in cablaggio, ma non "
    "c'è ancora una PR né …\n"
    "@clodia-354 riprendiamo il lavoro e pubblichiamo i due articoli"
)


class _ConRegistry(unittest.TestCase):
    """Registry locale: `clodia` a istanza unica, `worker` multi-spawn."""

    def setUp(self) -> None:
        self.agents = {
            "clodia": _a("clodia", "super", "P3", "2026-01-01T00:00:00Z"),
            "worker": _a("worker", "normal", "P1", "2026-02-01T00:00:00Z"),
            "security-engineer": _a("security-engineer", "normal", "P1",
                                    "2026-02-01T00:00:01Z"),
        }
        self.agents["worker"].multi_spawn = True
        self._orig = channels.registry.get_by_name
        channels.registry.get_by_name = lambda n: self.agents.get(n)

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig


class TheIncidentTests(_ConRegistry):
    """Il promemoria del 3 ott, ricomposto dal suo stesso codice."""

    def _promemoria(self) -> str:
        """Il messaggio come lo posta `goal_watch.tick`: `@orchestratore` +
        il testo di `promemoria()`. L'orchestratore è `clodia` (il
        `contact_agent` del canale), scritto qui alla lettera perché questo
        test misuri la CITAZIONE e non la normalizzazione dell'indirizzo."""
        voce = {"tier": "SEAL-1", "name": "tomato-blogging",
                "fermo_da_minuti": 48, "promemoria_n": 1,
                "goal": {"text": GOAL_INCIDENTE, "state": "pinned"}}
        return f"@clodia {goal_watch.promemoria(voce)}"

    def test_the_reminder_has_exactly_one_recipient(self) -> None:
        """Il caso della issue, alla lettera: `['clodia']`, non `['clodia', 'clodia-354']`."""
        self.assertEqual(["clodia"], mentions.extract_tags(self._promemoria()))
        self.assertEqual(["clodia"], channels._tags(self._promemoria()))

    def test_every_line_of_the_goal_is_quoted(self) -> None:
        """Non basta che il conto dei tag torni: le righe devono essere citate
        davvero, se no il prossimo obiettivo con un `@` diverso ci ricasca."""
        testo = self._promemoria()
        righe = [r for r in testo.splitlines() if "clodia-354" in r]
        self.assertTrue(righe, "il goal deve comparire nel promemoria")
        for r in righe:
            self.assertTrue(r.startswith(">"), f"riga non citata: {r!r}")

    def test_the_reminder_still_convokes_the_orchestrator(self) -> None:
        """La cura non deve rendere muto anche il `@` che serve: il promemoria
        senza destinatario sarebbe lo stesso guasto con un'altra causa."""
        self.assertEqual("clodia", channels._tagged(self._promemoria()))


class CitaTests(unittest.TestCase):
    """`cita()` — la citazione è di tutto il testo, non della prima riga."""

    def test_every_line_gets_the_marker(self) -> None:
        self.assertEqual("> a\n> b\n> c", mentions.cita("a\nb\nc"))

    def test_an_empty_line_keeps_the_block_together(self) -> None:
        """Una riga vuota non citata spezzerebbe il blockquote in due, e fra i
        due pezzi il testo torna normale — cioè le `@` tornano vive."""
        self.assertEqual("> a\n>\n> b", mentions.cita("a\n\nb"))

    def test_nothing_in_a_quoted_goal_convokes(self) -> None:
        self.assertEqual([], mentions.extract_tags(mentions.cita(GOAL_INCIDENTE)))

    def test_no_text_no_crash(self) -> None:
        self.assertEqual(">", mentions.cita(""))
        self.assertEqual(">", mentions.cita(None))


class ReportedOutcomeTests(unittest.IsolatedAsyncioTestCase):
    """Il resoconto di fine turno riporta il testo di un ALTRO: va citato tutto.

    `[turno concluso]` è fra i marcatori di `_RIPORTI`, ma quel taglio arriva a
    FINE RIGA — l'esito sta sotto, e una `@` alla sua terza riga convoca un
    terzo agente da un messaggio che la piattaforma firma come proprio. Stessa
    forma del difetto del promemoria, altro template: per questo la cura è la
    stessa funzione e non un secondo rattoppo.

    Il montaggio è quello di `test_480_resoconti_senza_sigillo`, che misura lo
    stesso testo dall'altro lato (il sigillo che il resoconto NON deve avere).
    """

    ESITO = ("fatto: la PR è aperta.\n"
             "ho chiesto a @sysadmin di guardare i log del deploy\n"
             "@fullstack-dev-9 ha il resto del contesto")

    async def _testo_del_resoconto(self) -> str:
        turns: list[tuple] = []

        async def start_turn(tier, name, tier_real, s, principal, text, kind, **kw):
            turns.append((s.name, text))
            return True

        ps = (
            patch.object(channels, "_start_turn", start_turn),
            patch.object(channels.registry, "get_by_name",
                         lambda n: _Spec("clodia") if n == "clodia" else None),
            patch.object(channels.topics_client, "open_topic",
                         lambda *_a, **_k: {"meta": {
                             "tier": "SEAL-1",
                             "participants": ["clodia", "fullstack-dev"]}}),
            patch.object(channels, "_provider_seal_ok", lambda *_a: True),
        )
        for p in ps:
            p.start()
        try:
            await channels._report_back("SEAL-1", "acme", "fullstack-dev",
                                        _Chat(CHAIN), self.ESITO, 0)
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(1, len(turns))
        return turns[0][1]

    async def test_a_mention_inside_the_reported_outcome_convokes_nobody(self) -> None:
        self.assertEqual([], channels._tags(await self._testo_del_resoconto()))

    async def test_the_outcome_is_still_readable_in_the_report(self) -> None:
        """Citare non è censurare: chi riceve il resoconto deve poterlo leggere."""
        testo = await self._testo_del_resoconto()
        self.assertIn("> fatto: la PR è aperta.", testo)
        self.assertIn("fullstack-dev", testo)


class IndirizzoTests(_ConRegistry):
    """La forma con cui la piattaforma indirizza un agente."""

    def test_a_single_spawn_seed_is_addressed_by_its_seed_name(self) -> None:
        self.assertEqual("clodia", channels.indirizzo("clodia-354"))
        self.assertEqual("clodia", channels.indirizzo("clodia"))

    def test_a_multi_spawn_seed_keeps_its_instance(self) -> None:
        self.assertEqual("worker-2", channels.indirizzo("worker-2"))
        self.assertEqual("worker", channels.indirizzo("worker"))

    def test_the_seed_boundary_is_the_registry_not_the_dash(self) -> None:
        """`security-engineer` è un seed col trattino: ridurlo per stringa
        darebbe `security`, che non esiste (#260, #286, #294)."""
        self.assertEqual("security-engineer", channels.indirizzo("security-engineer-1"))
        self.assertEqual("security-engineer", channels.indirizzo("security-engineer"))

    def test_an_unknown_name_is_left_alone(self) -> None:
        """Un umano che si chiama `mario-2` non diventa l'istanza 2 di `mario`."""
        self.assertEqual("mario-2", channels.indirizzo("mario-2"))
        self.assertEqual("", channels.indirizzo(None))


class RouterSafetyNetTests(_ConRegistry):
    """`@seed-N` scritto da una persona, o già scritto in un messaggio vecchio."""

    def test_the_router_resolves_a_spawn_label_to_its_seed(self) -> None:
        self.assertEqual(("clodia", None), channels._split_target("clodia-354"))

    def test_an_instance_of_a_multi_spawn_seed_still_addresses_it(self) -> None:
        self.assertEqual(("worker", "worker-2"), channels._split_target("worker-2"))

    def test_two_forms_of_one_agent_are_one_recipient(self) -> None:
        """È la riduzione da cui dipende R3: due identità distinte avrebbero
        aperto il dialogo «scegli clodia o clodia-354» al posto di un turno."""
        self.assertEqual(channels._target_identity("clodia"),
                         channels._target_identity("clodia-354"))
        self.assertEqual(["clodia"],
                         channels._distinct_by(["clodia", "clodia-354"],
                                               channels._target_identity))

    def test_two_instances_of_a_multi_spawn_seed_stay_two(self) -> None:
        self.assertEqual(["worker-2", "worker-3"],
                         channels._distinct_by(["worker-2", "worker-3"],
                                               channels._target_identity))


class GoalOnWriteTests(_ConRegistry):
    """L'obiettivo si salva già nella forma giusta (criterio 3 della issue)."""

    def test_a_live_spawn_label_is_normalised(self) -> None:
        g = topics._goal_indirizzato({"text": "@clodia-354 riprendiamo il lavoro"})
        self.assertEqual("@clodia riprendiamo il lavoro", g["text"])

    def test_the_rest_of_the_goal_is_untouched(self) -> None:
        g = topics._goal_indirizzato(
            {"text": "@clodia-354 vedi `@clodia-354` e\n> @clodia-354 diceva",
             "state": "pinned", "message_id": "m1"})
        self.assertEqual("@clodia vedi `@clodia-354` e\n> @clodia-354 diceva",
                         g["text"])
        self.assertEqual("pinned", g["state"])
        self.assertEqual("m1", g["message_id"])

    def test_a_multi_spawn_instance_survives_the_pin(self) -> None:
        g = topics._goal_indirizzato({"text": "@worker-2 fai tu"})
        self.assertEqual("@worker-2 fai tu", g["text"])

    def test_nothing_to_change_nothing_to_copy(self) -> None:
        goal = {"text": "@clodia procedi"}
        self.assertIs(goal, topics._goal_indirizzato(goal))
        self.assertIsNone(topics._goal_indirizzato(None))


class NormalizzaTests(unittest.TestCase):
    """`normalizza()` riscrive dove il parser legge, e solo lì."""

    @staticmethod
    def _sempre(_nome: str) -> str:
        return "clodia"

    def test_it_rewrites_a_live_mention(self) -> None:
        self.assertEqual("@clodia vai",
                         mentions.normalizza("@mario vai", self._sempre))

    def test_it_does_not_touch_code_quotes_or_reported_text(self) -> None:
        for testo in ("usa `@mario` come placeholder",
                      "```\n@mario guarda\n```",
                      "> @mario aveva scritto",
                      "Routing: scegli @mario o @anna.",
                      "scrivi a mario@bar.com"):
            with self.subTest(testo=testo):
                self.assertEqual(testo, mentions.normalizza(testo, self._sempre))

    def test_a_resolver_that_says_nothing_leaves_the_text_alone(self) -> None:
        self.assertEqual("@mario vai",
                         mentions.normalizza("@mario vai", lambda _n: None))
        self.assertEqual("@mario vai",
                         mentions.normalizza("@mario vai", lambda _n: ""))

    def test_the_rewrite_sees_the_same_text_the_parser_reads(self) -> None:
        """L'invariante detta a voce alta, su tutta la tabella condivisa:
        riscrivendo OGNI mention viva con lo stesso nome, chi convocava resta
        convocato (sotto un nome solo) e chi non convocava non si sveglia —
        nessuna `@` inerte riscritta, nessuna viva persa."""
        for testo, attesi in mentions.GOLDEN_CASES:
            with self.subTest(testo=testo):
                riscritto = mentions.normalizza(testo, self._sempre)
                self.assertEqual(["clodia"] if attesi else [],
                                 mentions.extract_tags(riscritto))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
