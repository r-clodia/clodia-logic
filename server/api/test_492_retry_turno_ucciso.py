"""Un turno ucciso prima di fare qualunque cosa si ritenta UNA volta
(clodia-platform#492).

Nella #492 il turno di clodia-400 muore con SIGKILL e nessuno lo ritenta: il
messaggio dell'owner resta non elaborato e in canale compare solo un errore.
Rimandarlo era corretto — il turno non aveva ancora prodotto niente — ma lo
doveva fare una persona, e solo dopo aver capito che quell'errore significava
«ucciso» e non «il modello non ha risposto».

Il vincolo che decide tutto è **zero eventi SDK**, e non è prudenza generica:
dal primo evento in poi il turno può aver già chiamato dei tool — mail spedite,
file scritti, messaggi postati — e ripeterlo rifarebbe quegli effetti. Un retry
che rimanda una mail è molto peggio di un turno perso.

Le altre due condizioni (sessione ricreata, morte del processo) escludono i casi
in cui il secondo tentativo morirebbe identico al primo.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from claude_agent_sdk._errors import ProcessError

from . import channels
from ..sdk_runtime.session import SessioneTerminata


def _ucciso(*, ripristinata: bool = True,
            eventi: "int | None" = 0) -> SessioneTerminata:
    """L'eccezione esatta della #492: ProcessError exit -9, cioè SIGKILL."""
    return SessioneTerminata(
        ProcessError("Command failed with exit code -9", exit_code=-9),
        ripristinata, segnale=9, eventi=eventi)


class _Chat:
    """Sessione finta che muore le prime N volte e poi risponde."""

    principal = ""

    def __init__(self, errori, reply="fatto"):
        self.errori = list(errori)
        self.reply = reply
        self.chiamate = 0

    async def send_user_message(self, _prompt: str) -> str:
        self.chiamate += 1
        if self.errori:
            raise self.errori.pop(0)
        return self.reply


class _RetryHarness(unittest.IsolatedAsyncioTestCase):
    """Il montaggio di `_run_and_post_response` con tutte le cuciture finte.

    Separato dai test perché lo riusa anche il turno opencode scaduto
    (`test_514_retry_turno_scaduto`): è lo stesso percorso, con un'altra
    eccezione in ingresso.
    """

    def setUp(self) -> None:
        self.posts: list[tuple[str, str]] = []
        self.messages: list[dict] = []
        self.annunci: list[dict] = []
        self.bg: list[asyncio.Task] = []

        def post(_tier, _name, author, text, kind="human", **_kw):
            row = {"id": str(len(self.messages) + 1), "author": author,
                   "text": text, "kind": kind, "ts": str(len(self.messages) + 1)}
            self.messages.append(row)
            self.posts.append((author, text))
            return row

        async def spy_announce(_tier, _name, responder, err, **kw):
            self.annunci.append({"responder": responder, "err": err, **kw})

        async def noop_async(*_a, **_kw):
            return None

        def run_bg(coro):
            self.bg.append(asyncio.ensure_future(coro))

        self._patches = [
            patch.object(channels.topics_client, "post_message", post),
            patch.object(channels.topics_client, "list_messages",
                         lambda *_a, **_kw: list(self.messages)),
            patch.object(channels, "_maybe_delegate", noop_async),
            patch.object(channels, "_typing", noop_async),
            patch.object(channels, "_channel_message", noop_async),
            patch.object(channels, "_watch_report", noop_async),
            patch.object(channels, "_topic_title", lambda *_a, **_kw: None),
            patch.object(channels, "_announce_failure", spy_announce),
            patch.object(channels, "_spawn_bg", run_bg),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    async def _run(self, chat):
        esito = await channels._run_and_post_response("P0", "ops", "clodia", chat, "prompt")
        if self.bg:
            await asyncio.gather(*self.bg)
        return esito


class RetryPolicyTests(_RetryHarness):

    async def test_un_turno_ucciso_a_vuoto_viene_ritentato(self) -> None:
        """IL DIFETTO, in forma di test: oggi il turno muore e basta — una
        chiamata sola, nessuna risposta nel canale, un annuncio d'errore."""
        chat = _Chat([_ucciso()])
        esito = await self._run(chat)
        self.assertEqual(2, chat.chiamate, "il turno ucciso non è stato ritentato")
        self.assertEqual("fatto", esito)
        self.assertIn(("clodia", "fatto"), self.posts)
        self.assertEqual([], self.annunci, "ritentato con successo: niente da annunciare")

    async def test_un_turno_che_aveva_gia_lavorato_non_si_ritenta(self) -> None:
        """Il vincolo che rende il retry sicuro. Un turno con eventi alle spalle
        può aver già chiamato dei tool: ripeterlo rifarebbe quegli effetti, e
        nessun messaggio salvato vale una mail spedita due volte."""
        chat = _Chat([_ucciso(eventi=3)])
        await self._run(chat)
        self.assertEqual(1, chat.chiamate)
        self.assertEqual(1, len(self.annunci))
        self.assertFalse(self.annunci[0]["ritentato"])

    async def test_non_sapere_quanti_eventi_vale_quanto_averne_avuti(self) -> None:
        """«Non so» non è «zero». Il rimando è innocuo solo quando la prova
        c'è, e l'assenza della prova non è una prova: chi costruisce
        l'eccezione senza il conto non deve ottenere il rimando per difetto."""
        chat = _Chat([_ucciso(eventi=None)])
        await self._run(chat)
        self.assertEqual(1, chat.chiamate)
        self.assertEqual(1, len(self.annunci))

    async def test_senza_sessione_ricreata_non_si_ritenta(self) -> None:
        """Il secondo tentativo finirebbe nello stesso subprocess morto."""
        chat = _Chat([_ucciso(ripristinata=False)])
        await self._run(chat)
        self.assertEqual(1, chat.chiamate)
        self.assertEqual(1, len(self.annunci))

    async def test_un_errore_qualunque_non_si_ritenta(self) -> None:
        """Il confine: il retry vale per un turno UCCISO, non per un turno
        andato male. Un errore che si ripresenterebbe identico raddoppierebbe
        solo il tempo prima di dirlo a chi aspetta."""
        chat = _Chat([RuntimeError("il modello ha risposto male")])
        await self._run(chat)
        self.assertEqual(1, chat.chiamate)
        self.assertEqual(1, len(self.annunci))

    async def test_si_ritenta_una_volta_sola(self) -> None:
        """Due uccisioni di fila non diventano un ciclo: il secondo fallimento
        si annuncia, dichiarando che il retry c'è già stato."""
        chat = _Chat([_ucciso(), _ucciso()])
        await self._run(chat)
        self.assertEqual(2, chat.chiamate)
        self.assertEqual(1, len(self.annunci))
        self.assertTrue(self.annunci[0]["ritentato"])


class TheSecondFailureSaysTheRetryHappenedTests(unittest.IsolatedAsyncioTestCase):
    """Senza questa riga la nota della sessione terminata dice «basta
    rimandarlo» a chi lo ha già visto rimandare: il secondo fallimento
    sembrerebbe il primo e la cura suggerita sarebbe quella appena fallita."""

    async def _annuncia(self, ritentato: bool) -> str:
        posts: list[str] = []

        def post(_tier, _name, _author, text, kind="human", **_kw):
            posts.append(text)
            return {"id": "1", "text": text}

        async def noop_async(*_a, **_kw):
            return None

        with patch.object(channels.topics_client, "post_message", post), \
             patch.object(channels, "_channel_message", noop_async), \
             patch.object(channels, "_topic_title", lambda *_a, **_kw: None), \
             patch.object(channels, "_store_reasoning", lambda *_a, **_kw: None), \
             patch.object(channels.registry, "get_by_name", lambda *_a, **_kw: None):
            await channels._announce_failure("P0", "ops", "clodia", _ucciso(),
                                             ritentato=ritentato)
        return "\n".join(posts)

    async def test_lo_dichiara_quando_e_successo(self) -> None:
        testo = await self._annuncia(True)
        self.assertIn("ritentato", testo.lower())

    async def test_e_tace_quando_non_e_successo(self) -> None:
        testo = await self._annuncia(False)
        self.assertNotIn("ritentato", testo.lower())
        # Il resto del messaggio resta quello di prima: il turno ucciso si
        # racconta comunque, ed è la parte che la #492 chiedeva.
        self.assertIn("terminato dall'esterno", testo)
