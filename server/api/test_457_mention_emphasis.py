"""clodia-logic#457 · `@nome` dentro `**grassetto**` / `*corsivo*` è una menzione.

Il sintomo misurato nella issue: la lookbehind di `mentions.py` ammette il
sigillo solo dopo inizio stringa, whitespace o punteggiatura di apertura, e
l'asterisco dell'enfasi markdown non era nell'elenco.

    '**@sysadmin** guarda' -> []      # persa
    '*@clodia* nota'       -> []      # persa

Il danno è doppio, e si è visto sul messaggio `20260914-145721-UF3ebA`
(origine di clodia-platform#367): il destinatario vero non riceve l'ordine, e
al suo posto viene agganciato un altro nome presente nel testo — cioè si apre
un turno a chi non c'entra.

Questa matrice sta in un modulo suo, separata dal fix di routing di
clodia-logic#445: la regex governa le menzioni di OGNI messaggio della
piattaforma, e un revert di quel fix non deve portarsi via anche questo.

I casi entrano in `mentions.GOLDEN_CASES` — la tabella che viaggia dentro il
modulo e che la suite di ENTRAMBI i repository esegue sui propri entry point —
e qui vengono ri-misurati sugli entry point del router (`channels._tags`,
`channels._tagged`), che sono quelli che decidono se un turno parte davvero.
"""
from __future__ import annotations

import unittest

from . import channels, mentions

#: `(testo, attesi)`. Le prime due righe sono le due misurate nella issue.
EMPHASIS_CASES: tuple[tuple[str, list[str]], ...] = (
    # ── il sintomo della issue, alla lettera ────────────────────────────────
    ("**@sysadmin** guarda", ["sysadmin"]),
    ("*@clodia* nota", ["clodia"]),
    # ── grassetto e corsivo, anche a fine frase e con la punteggiatura ──────
    ("chiedi a **@clodia** di guardare", ["clodia"]),
    ("chiedi a *@clodia*, poi vedi", ["clodia"]),
    ("**@fullstack-dev#2** prendila tu", ["fullstack-dev#2"]),
    ("**@tomato.fullstack-dev** vai", ["tomato.fullstack-dev"]),
    # ── annidati e combinati ────────────────────────────────────────────────
    ("***@davide*** decide", ["davide"]),
    ("_**@anna**_ ok", ["anna"]),
    ("**@clodia** poi *@mario*", ["clodia", "mario"]),
    ("(**@davide**) e [*@luca*]", ["davide", "luca"]),
    # ── quello che NON deve diventare una menzione ──────────────────────────
    # L'asterisco apre il confine, non lo cancella: dentro l'enfasi valgono
    # ancora tutte le regole di #255 e #391.
    ("**scrivi a foo@bar.com**", []),
    ("*a@clodia.io*", []),
    ("**$clodia**", []),
    ("*$mario avvisa*", []),
    # Il caso preesistente citato dalla issue: `$100` non è una menzione, né
    # prima né dopo questa correzione.
    ("costa $100 in tutto", []),
    ("**costa $100 in tutto**", []),
    # ── codice e citazioni continuano a vincere sull'enfasi ─────────────────
    ("usa `**@clodia**` come placeholder", []),
    ("```\n**@clodia** guarda\n```", []),
    ("> **@clodia** aveva scritto così", []),
    ("> *@clodia* diceva\nrispondo io: **@luca**", ["luca"]),
)


class EmphasisIsAWordBoundaryTests(unittest.TestCase):
    """La matrice, sul parser condiviso."""

    def test_extract_mentions(self) -> None:
        for testo, attesi in EMPHASIS_CASES:
            with self.subTest(testo=testo):
                self.assertEqual(attesi, mentions.extract_mentions(testo))

    def test_extract_tags(self) -> None:
        for testo, attesi in EMPHASIS_CASES:
            with self.subTest(testo=testo):
                self.assertEqual(attesi, mentions.extract_tags(testo))

    def test_the_cases_travel_in_the_shared_table(self) -> None:
        """Se restassero solo qui, l'altra copia (`clodia-tools`) potrebbe
        divergere in silenzio: il golden è il solo canale fra i due repo."""
        for caso in EMPHASIS_CASES:
            with self.subTest(testo=caso[0]):
                self.assertIn(caso, mentions.GOLDEN_CASES)


class TheRouterEntryPointsAgreeTests(unittest.TestCase):
    """Gli stessi casi da dove si decide chi prende il turno."""

    def test_tags(self) -> None:
        for testo, attesi in EMPHASIS_CASES:
            with self.subTest(testo=testo):
                self.assertEqual(attesi, channels._tags(testo))

    def test_tagged_returns_the_first_hard_tag_or_nothing(self) -> None:
        for testo, attesi in EMPHASIS_CASES:
            with self.subTest(testo=testo):
                self.assertEqual(attesi[0] if attesi else None,
                                 channels._tagged(testo))


class TheMessageThatOriginatedTheIssueTests(unittest.TestCase):
    """`20260914-145721-UF3ebA`: menzione in grassetto persa, `$nome` citato
    nel testo agganciato al suo posto.

    Il secondo effetto — il turno aperto a chi non c'entra — non era un secondo
    difetto: era la conseguenza del primo, perché `$clodia` non è mai stato una
    menzione (#391) e restava l'unico nome che il router riusciva a leggere.
    """

    def test_the_bold_mention_is_the_one_that_is_served(self) -> None:
        testo = "**@sysadmin** guarda il log, $clodia l'aveva già chiesto"
        self.assertEqual(["sysadmin"], channels._tags(testo))
        self.assertEqual("sysadmin", channels._tagged(testo))


if __name__ == "__main__":
    unittest.main()
