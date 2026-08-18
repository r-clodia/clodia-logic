"""Un turno può valere più bolle, e ogni bolla resta dove è comparsa.

clodia-platform#243. «Un turno una bolla» non è mai stata una regola: era il
punto in cui cadeva il post. Il testo di un turno veniva accumulato e pubblicato
in fondo (`channels.py`, `topics_client.post_message` unico), mentre lo stesso
testo era già stato mostrato in streaming come una bolla a tutti gli effetti —
avatar, autore, badge «sta rispondendo».

Le conseguenze erano due, e la seconda è una perdita di dati:

1. la bolla in streaming spariva a metà turno, perché il frontend leggeva
   «è arrivato un messaggio di quell'autore» come «il turno è finito»;
2. se l'agente aveva postato qualcosa via tool durante il turno, la risposta
   finale veniva **soppressa** — e con essa il testo streamato che nessun tool
   aveva pubblicato. Chi guardava lo aveva letto, poi non c'era più.

La soppressione era giusta (non ripubblicare ciò che è già nel canale); il
difetto era che il testo streamato non fosse *anch'esso* nel canale. Ora ogni
blocco chiuso è un messaggio nel momento in cui compare: l'agente può rispondere
subito e continuare a lavorare, e ciò che ha detto resta.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from . import channels
from ..sdk_runtime.session import _BlockFilter


class BlockFilterReportsTheSeamTests(unittest.TestCase):
    """Il confine fra due bolle lo conosce solo `_BlockFilter`.

    `feed` inserisce `\\n\\n` fra blocchi tenuti, quindi chi legge il valore di
    ritorno non può più dire dove finiva l'uno e cominciava l'altro. Ricostruire
    la cucitura nel chiamante vorrebbe dire tenerne due copie — ed è così che
    divergono. Questi test guardano `pop_completed`, che è la cucitura resa
    esplicita.
    """

    def test_two_blocks_are_two_bubbles(self) -> None:
        f = _BlockFilter()
        f.feed(0, "Sì, guardo subito.")
        f.end_block()
        f.feed(1, "Confermato: è in db.py.")
        f.end_block()
        self.assertEqual(["Sì, guardo subito.", "Confermato: è in db.py."],
                         f.pop_completed())

    def test_the_separator_is_not_part_of_the_bubble(self) -> None:
        """`\\n\\n` serve alla vista concatenata, non alla bolla: una bolla che
        comincia con due righe vuote è un difetto visibile."""
        f = _BlockFilter()
        f.feed(0, "primo")
        f.end_block()
        visibile = f.feed(1, "secondo")
        self.assertTrue(visibile.startswith("\n\n"), "il flusso perde il separatore")
        f.end_block()
        for bolla in f.pop_completed():
            self.assertFalse(bolla.startswith("\n"), f"bolla con capo a vuoto: {bolla!r}")

    def test_deltas_of_one_block_stay_one_bubble(self) -> None:
        """Il confine è il BLOCCO, non il delta: altrimenti ogni token sarebbe
        una bolla."""
        f = _BlockFilter()
        for pezzo in ("Ho", " guardato", " il", " codice."):
            f.feed(0, pezzo)
        f.end_block()
        self.assertEqual(["Ho guardato il codice."], f.pop_completed())

    def test_an_injected_block_is_no_bubble(self) -> None:
        """L'iniezione del runtime (SKILL.md espansa) era già filtrata dal
        flusso: non deve rientrare dalla porta delle bolle."""
        f = _BlockFilter()
        f.feed(0, "Base directory for this skill: /qualcosa\nresto della skill")
        f.end_block()
        self.assertEqual([], f.pop_completed())

    def test_taking_them_twice_does_not_repeat_them(self) -> None:
        """`pop_completed` svuota: chi li prende li posta, e un blocco
        consegnato due volte sarebbe una bolla duplicata."""
        f = _BlockFilter()
        f.feed(0, "una volta")
        f.end_block()
        self.assertEqual(["una volta"], f.pop_completed())
        self.assertEqual([], f.pop_completed())


class _Chat:
    """Sessione finta che consegna blocchi come farebbe `_collect_response`."""

    principal = ""

    def __init__(self, blocchi, reply=None, post_via_tool=None):
        self.blocchi = list(blocchi)
        self._reply = reply
        self.post_via_tool = post_via_tool

    async def send_user_message(self, _prompt: str) -> str:
        if self.post_via_tool:
            channels.topics_client.post_message(
                "P0", "ops", "clodia", self.post_via_tool, kind="ai")
        cb = getattr(self, "on_visible_block", None)
        if cb is not None:
            for b in self.blocchi:
                await cb(b)
        return self._reply if self._reply is not None else "\n\n".join(self.blocchi)


class BubblesArePostedAsTheyAppearTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.posts: list[tuple[str, str, str]] = []
        self.messages: list[dict] = []
        self.delegated: list[str] = []

        def post(_tier, _name, author, text, kind="human", **_kw):
            row = {"id": str(len(self.messages) + 1), "author": author,
                   "text": text, "kind": kind, "ts": str(len(self.messages) + 1)}
            self.messages.append(row)
            self.posts.append((author, text, kind))
            return row

        async def spy_delegate(_tier, _name, _responder, text, *_a, **_kw):
            self.delegated.append(text)

        async def noop_async(*_a, **_kw):
            return None

        self._patches = [
            patch.object(channels.topics_client, "post_message", post),
            patch.object(channels.topics_client, "list_messages",
                         lambda *_a, **_kw: list(self.messages)),
            patch.object(channels, "_maybe_delegate", spy_delegate),
            patch.object(channels, "_typing", noop_async),
            patch.object(channels, "_channel_message", noop_async),
            patch.object(channels, "_topic_title", lambda *_a, **_kw: None),
            patch.object(channels, "_spawn_bg", lambda _c: _c.close()),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    async def _run(self, chat):
        return await channels._run_and_post_response("P0", "ops", "clodia", chat, "prompt")

    async def test_each_block_becomes_its_own_message(self) -> None:
        """Il caso che Davide descrive: risposta subito, poi il lavoro, poi
        l'esito. Tre bolle, nell'ordine in cui sono comparse."""
        with patch.dict(os.environ, {"CLODIA_BUBBLE_PER_BLOCK": "1"}):
            await self._run(_Chat(["Sì, guardo subito.",
                                   "Confermato: è in db.py.",
                                   "Fatto, PR aperta."]))
        self.assertEqual(
            [("clodia", "Sì, guardo subito.", "ai"),
             ("clodia", "Confermato: è in db.py.", "ai"),
             ("clodia", "Fatto, PR aperta.", "ai")],
            self.posts)

    async def test_the_final_reply_is_not_posted_again(self) -> None:
        """La risposta finale è la concatenazione delle bolle: ripubblicarla
        sarebbe il testo due volte, che è il difetto opposto a quello curato."""
        with patch.dict(os.environ, {"CLODIA_BUBBLE_PER_BLOCK": "1"}):
            await self._run(_Chat(["primo", "secondo"]))
        self.assertEqual(2, len(self.posts), f"bolla di troppo: {self.posts}")
        self.assertNotIn("primo\n\nsecondo", [t for _a, t, _k in self.posts])

    async def test_every_bubble_gets_its_mention_served(self) -> None:
        """Una menzione per bolla. Se l'agente tagga @X nel primo blocco e di
        nuovo nell'ultimo, X riceve due turni: è la regola dei messaggi umani
        (un messaggio, un turno) applicata a messaggi che ora sono più d'uno.
        Dichiarato in un test perché con #243 smette di essere un caso raro."""
        with patch.dict(os.environ, {"CLODIA_BUBBLE_PER_BLOCK": "1"}):
            await self._run(_Chat(["@worker comincia tu", "ho finito, @worker chiudi"]))
        self.assertEqual(["@worker comincia tu", "ho finito, @worker chiudi"],
                         self.delegated)

    async def test_streamed_text_is_not_lost_when_a_tool_also_posts(self) -> None:
        """IL DIFETTO, in forma di test.

        L'agente posta qualcosa via tool E dice qualcosa in streaming. Prima la
        risposta finale veniva soppressa e il testo streamato spariva: era stato
        letto da chi guardava e non era in nessun messaggio. Ora è una bolla.
        """
        with patch.dict(os.environ, {"CLODIA_BUBBLE_PER_BLOCK": "1"}):
            await self._run(_Chat(["Ecco cosa ho trovato: il bug è in db.py."],
                                  post_via_tool="Report allegato al topic"))
        testi = [t for _a, t, _k in self.posts]
        self.assertIn("Report allegato al topic", testi)
        self.assertIn("Ecco cosa ho trovato: il bug è in db.py.", testi,
                      "il testo streamato è stato buttato: è il difetto di #243")

    async def test_a_blank_block_is_no_bubble(self) -> None:
        with patch.dict(os.environ, {"CLODIA_BUBBLE_PER_BLOCK": "1"}):
            await self._run(_Chat(["   \n  ", "vero contenuto"]))
        self.assertEqual([("clodia", "vero contenuto", "ai")], self.posts)

    async def test_with_the_flag_off_it_is_one_bubble_as_before(self) -> None:
        """Rollback senza ricompilare: la callback non viene registrata, la
        sessione non consegna nulla e la risposta finale torna a essere un
        messaggio solo."""
        with patch.dict(os.environ, {"CLODIA_BUBBLE_PER_BLOCK": "0"}):
            chat = _Chat(["primo", "secondo"])
            await self._run(chat)
        self.assertEqual([("clodia", "primo\n\nsecondo", "ai")], self.posts)
        self.assertIsNone(getattr(chat, "on_visible_block", None))

    async def test_the_callback_does_not_survive_the_turn(self) -> None:
        """La sessione è di lunga vita: un riferimento lasciato attaccato
        posterebbe le bolle del turno successivo col label di questo."""
        with patch.dict(os.environ, {"CLODIA_BUBBLE_PER_BLOCK": "1"}):
            chat = _Chat(["unica"])
            await self._run(chat)
        self.assertIsNone(getattr(chat, "on_visible_block", None))

    async def test_a_failed_bubble_does_not_kill_the_turn(self) -> None:
        """Se il post di una bolla fallisce il turno continua: la bolla è un
        effetto collaterale della raccolta, perderne una è meno grave che
        perdere la risposta."""
        def post_rotto(*_a, **_kw):
            raise RuntimeError("topic store giù")

        with patch.dict(os.environ, {"CLODIA_BUBBLE_PER_BLOCK": "1"}), \
                patch.object(channels.topics_client, "post_message", post_rotto):
            chat = _Chat(["qualcosa"])
            # nessuna eccezione deve uscire da qui
            await channels._run_and_post_response("P0", "ops", "clodia", chat, "prompt")


class TheFlagIsReadableTests(unittest.TestCase):
    def test_default_is_on(self) -> None:
        """La semantica scelta è questa: il default la applica, e chi installa
        non deve accendere niente per averla."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLODIA_BUBBLE_PER_BLOCK", None)
            self.assertTrue(channels._bubble_per_block())

    def test_the_off_switches_understood(self) -> None:
        for spento in ("0", "false", "no", "off", "OFF", "False"):
            with self.subTest(valore=spento):
                with patch.dict(os.environ, {"CLODIA_BUBBLE_PER_BLOCK": spento}):
                    self.assertFalse(channels._bubble_per_block())

    def test_anything_else_is_on(self) -> None:
        """Un valore illeggibile non spegne una semantica: come per
        `CLODIA_MAX_DELEGATION_HOPS`, si ricade sul default invece di
        interpretare a caso."""
        for acceso in ("1", "true", "yes", "banana", ""):
            with self.subTest(valore=acceso):
                with patch.dict(os.environ, {"CLODIA_BUBBLE_PER_BLOCK": acceso}):
                    self.assertTrue(channels._bubble_per_block())


if __name__ == "__main__":
    unittest.main()
