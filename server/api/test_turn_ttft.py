"""Il silenzio prima della prima parola: renderlo visibile e misurarlo.

clodia-platform#330, figlia di #228. La issue attribuisce l'attesa iniziale al
contesto da rileggere («400k token prima della prima parola»), ma nel percorso
di un turno la rilettura del modello è l'ULTIMA di sei attese seriali, e le
prime cinque non erano né misurate né visibili:

    routing (embedding)  →  attesa di uno spawn libero  →  creazione sessione
    →  costruzione prompt (storia dal gateway)  →  storia PRE-turno (500 msg)
    →  coda sul lock della sessione  →  query  →  [rilettura del modello]

Due difetti distinti, e questi test guardano l'uno e l'altro:

1. **Visibilità.** L'unico segnale che dice «il turno è partito» è l'evento
   `channel_typing`, e in `_run_and_post_response` veniva emesso DOPO la fetch
   della storia del canale (fino a 500 messaggi dal gateway). Finché quella
   chiamata è in volo il canale è muto: chi guarda non distingue un turno
   partito da una menzione caduta nel vuoto. Il segnale deve precedere
   qualunque I/O della funzione, perché è la ragione per cui esiste.

2. **Misura.** Senza le durate per fase, «il collo è la rilettura del contesto»
   resta un'ipotesi, e ottimizzare la fase sbagliata è il modo più caro di
   chiudere questa issue.
"""
from __future__ import annotations

import inspect
import logging
import unittest

from . import channels
from ..core import turn_timing
from ..sdk_runtime import session as S


class TheVisibleSignalComesFirstTests(unittest.IsolatedAsyncioTestCase):
    """Il segnale di typing precede ogni I/O di `_run_and_post_response`."""

    async def asyncSetUp(self) -> None:
        self.tracce: list[str] = []
        self._orig = {
            "list": channels.topics_client.list_messages,
            "post": channels.topics_client.post_message,
            "typing": channels._typing,
            "delegate": channels._maybe_delegate,
            "channel_message": channels._channel_message,
            "activity": channels.activity_log.append,
            "title": channels._topic_title,
        }

        def list_messages(*_a, **_k):
            self.tracce.append("storia")
            return []

        async def typing(_tier, _name, _agent, state):
            self.tracce.append(f"typing:{state}")

        async def noop_async(*_a, **_k):
            return None

        channels.topics_client.list_messages = list_messages
        channels.topics_client.post_message = (
            lambda _tier, _name, author, text, kind="human", **_k:
                {"id": "1", "author": author, "text": text, "kind": kind})
        channels._typing = typing
        channels._maybe_delegate = noop_async
        channels._channel_message = noop_async
        channels.activity_log.append = lambda *_a, **_k: None
        channels._topic_title = lambda *_a, **_k: None

    async def asyncTearDown(self) -> None:
        channels.topics_client.list_messages = self._orig["list"]
        channels.topics_client.post_message = self._orig["post"]
        channels._typing = self._orig["typing"]
        channels._maybe_delegate = self._orig["delegate"]
        channels._channel_message = self._orig["channel_message"]
        channels.activity_log.append = self._orig["activity"]
        channels._topic_title = self._orig["title"]

    class _Chat:
        principal = ""
        chat_id = "chan:P0:ops:clodia"

        async def send_user_message(self, _prompt: str) -> str:
            return "fatto"

    async def test_typing_starts_before_the_history_fetch(self) -> None:
        await channels._run_and_post_response("P0", "ops", "clodia", self._Chat(), "prompt")

        self.assertIn("typing:start", self.tracce, "nessun segnale di turno partito")
        self.assertIn("storia", self.tracce, "il test non ha intercettato la fetch")
        self.assertLess(
            self.tracce.index("typing:start"), self.tracce.index("storia"),
            "il segnale visibile arriva dopo la fetch della storia: finché quella "
            "chiamata è in volo il canale resta muto",
        )

    async def test_typing_starts_before_anything_else_at_all(self) -> None:
        """Non «prima della storia» per caso: prima di TUTTO.

        La fetch di oggi è una `list_messages`; domani può essere un'altra
        lettura. Ancorare l'asserzione alla prima traccia qualunque essa sia
        tiene la proprietà anche quando il corpo della funzione cambia.
        """
        await channels._run_and_post_response("P0", "ops", "clodia", self._Chat(), "prompt")

        self.assertEqual("typing:start", self.tracce[0],
                         f"la prima cosa che fa il turno è {self.tracce[0]!r}")


class PhasesAddUpToTheTotalTests(unittest.TestCase):
    """La somma delle voci fa il totale, sempre.

    È la proprietà che rende la riga leggibile: se le fasi non chiudessero il
    conto, un'attesa dimenticata sparirebbe dalla telemetria invece di
    apparire. Il resto non attribuito sta in `model_ms`, quindi una fase che
    nessuno marca lo gonfia e si nota.
    """

    def test_the_line_carries_every_phase_and_the_total(self) -> None:
        t = turn_timing.begin("start_turn")
        t.mark("routing")
        t.bind("chan:P0:ops:tester")
        t.mark("session_ready")
        t.mark("prompt_build")
        riga = t.line()

        for atteso in ("ttft_ms=", "routing_ms=", "session_ready_ms=",
                       "prompt_build_ms=", "model_ms=", "origin=start_turn"):
            self.assertIn(atteso, riga)
        self.assertIn("chan:P0:ops:tester", riga)
        turn_timing.drop(t)

    def test_the_unattributed_remainder_is_the_model(self) -> None:
        t = turn_timing.begin("topic_turn")
        t.mark("routing")
        valori = dict(
            pezzo.split("=") for pezzo in t.line().split() if "=" in pezzo
        )
        totale = float(valori["ttft_ms"])
        noto = float(valori["routing_ms"])
        modello = float(valori["model_ms"])
        self.assertAlmostEqual(totale, noto + modello, delta=2.0)

    def test_the_session_claims_the_turn_the_dispatcher_handed_over(self) -> None:
        """Il salto dispatcher → sessione non ha una firma: passa dalla
        consegna, e la sessione ritira il cronometro dal `chat_id`."""
        t = turn_timing.begin("start_turn")
        t.mark("prompt_build")
        t.bind("chan:P0:ops:consegna")

        ritirato = turn_timing.claim("chan:P0:ops:consegna")
        self.assertIs(t, ritirato)
        turn_timing.mark(ritirato, "queue_wait")
        self.assertIn("queue_wait_ms=", t.line())
        self.assertEqual(0, turn_timing.pending("chan:P0:ops:consegna"),
                         "il ritiro non ha svuotato la consegna: un secondo "
                         "turno ritirerebbe questo cronometro")

    def test_two_queued_turns_take_one_each_in_order(self) -> None:
        """Due turni accodati sulla STESSA sessione: ognuno il proprio.

        Con una consegna a slot singolo il secondo `bind` sovrascriveva il
        primo, e il turno che prendeva il lock per primo si trovava a chiudere
        il cronometro dell'altro — un TTFT più corto del vero, proprio nel caso
        in cui c'è una coda da misurare. La coda è FIFO come il lock.
        """
        primo = turn_timing.begin("start_turn")
        secondo = turn_timing.begin("start_turn")
        primo.bind("chan:P0:ops:coda")
        secondo.bind("chan:P0:ops:coda")

        self.assertIs(primo, turn_timing.claim("chan:P0:ops:coda"))
        self.assertIs(secondo, turn_timing.claim("chan:P0:ops:coda"))
        self.assertIsNone(turn_timing.claim("chan:P0:ops:coda"))

    def test_a_handover_nobody_claimed_is_withdrawn(self) -> None:
        """Se l'invio fallisce prima del ritiro, la voce non resta in coda: il
        turno dopo troverebbe un cronometro partito minuti prima e scriverebbe
        un TTFT inventato."""
        t = turn_timing.begin("start_turn")
        t.bind("chan:P0:ops:mai-ritirato")
        turn_timing.drop(t)
        self.assertIsNone(turn_timing.claim("chan:P0:ops:mai-ritirato"))
        turn_timing.drop(t)  # idempotente: nel caso normale la sessione l'ha già preso

    def test_a_turn_without_a_stopwatch_is_silent(self) -> None:
        """Un percorso che non passa dal dispatcher non è un errore: la
        telemetria non deve poter rompere un turno."""
        self.assertIsNone(turn_timing.claim("chan:P0:ops:mai-visto"))
        turn_timing.mark(None, "queue_wait")
        turn_timing.first_token(None)


class TheStopwatchClosesOnceTests(unittest.TestCase):

    def test_the_first_token_writes_exactly_one_line(self) -> None:
        t = turn_timing.begin("start_turn")
        t.mark("prompt_build")
        with self.assertLogs("agent-server.turn_timing", level=logging.INFO) as log:
            turn_timing.first_token(t)
            turn_timing.first_token(t)  # il delta successivo dello stesso turno
        self.assertEqual(1, len(log.records), "una riga per turno, non una per delta")
        self.assertIn("ttft_ms=", log.output[0])

    def test_a_turn_without_a_token_writes_nothing(self) -> None:
        """Errore, timeout, watchdog: un TTFT che non è mai arrivato non è una
        misura, e una riga con un numero inventato sarebbe peggio del silenzio."""
        t = turn_timing.begin("start_turn")
        t.mark("prompt_build")
        logger = logging.getLogger("agent-server.turn_timing")
        with self.assertLogs(logger, level=logging.INFO) as log:
            logger.info("sentinella")  # assertLogs pretende almeno un record
            del t
        self.assertEqual(["INFO:agent-server.turn_timing:sentinella"], log.output)

    def test_the_handover_has_a_ceiling_on_keys(self) -> None:
        """Una consegna che cresce con i turni è una perdita di memoria lenta:
        i turni che non arrivano mai alla sessione esistono."""
        for i in range(turn_timing._MAX_KEYS + 50):
            turn_timing.begin("stress").bind(f"chan:P0:ops:s{i}")
        self.assertLessEqual(len(turn_timing._PENDING), turn_timing._MAX_KEYS)


class TheTurnPathIsInstrumentedTests(unittest.TestCase):
    """Il cablaggio, dove guidare il codice costerebbe più di quanto misura.

    Stessa tecnica del test di A13 su `_collect_response`: le fasi vivono in
    quattro punti di due moduli, e ciò che si rompe è che un punto non chiami
    più il cronometro. È esattamente ciò che l'ispezione vede.
    """

    def test_both_dispatchers_open_the_stopwatch(self) -> None:
        for funzione in (channels._start_turn, channels.run_topic_turn):
            with self.subTest(dispatcher=funzione.__name__):
                src = inspect.getsource(funzione)
                self.assertIn("turn_timing.begin(", src)
                self.assertIn('mark("session_ready")', src)
                self.assertIn('mark("prompt_build")', src)
                self.assertIn("timing=timing", src,
                              "il cronometro non arriva al turno: le fasi del "
                               "dispatcher resterebbero senza primo token")

    def test_the_turn_hands_the_stopwatch_over_and_withdraws_it(self) -> None:
        src = inspect.getsource(channels._run_and_post_response)
        self.assertIn("timing.bind(", src, "senza consegna la sessione non ritrova il turno")
        self.assertIn("turn_timing.drop(timing)", src,
                      "una consegna mai ritirata resta in coda e falsa il turno dopo")

    def test_every_runtime_claims_the_stopwatch_not_just_claude(self) -> None:
        """I tre runtime hanno tre `send_user_message` diversi: strumentarne uno
        solo darebbe numeri per gli agent su claude e nessuno per messaggero e
        segretario, che girano su opencode."""
        for cls in (S.ChatSession, S.CodexChatSession, S.OpenCodeChatSession):
            with self.subTest(runtime=cls.__name__):
                src = inspect.getsource(cls.send_user_message)
                self.assertIn("turn_timing.claim(self.chat_id)", src)
                self.assertIn('turn_timing.mark(self._timing, "queue_wait")', src)

    def test_the_session_marks_the_query(self) -> None:
        src = inspect.getsource(S.ChatSession.send_user_message)
        self.assertIn('turn_timing.mark(self._timing, "query_sent")', src)

    def test_the_first_sign_of_life_closes_the_stopwatch(self) -> None:
        """Un punto per runtime, quello che vede il progresso — gli stessi tre
        del test di A13 su `last_activity`."""
        for cls, metodo in ((S.ChatSession, "_collect_response"),
                            (S.CodexChatSession, "_handle_event"),
                            (S.OpenCodeChatSession, "_handle_parts")):
            with self.subTest(runtime=cls.__name__):
                src = inspect.getsource(getattr(cls, metodo))
                self.assertIn("turn_timing.first_token(self._timing)", src)

    def test_the_stopwatch_does_not_survive_its_turn(self) -> None:
        """La sessione è di lunga vita: un cronometro lasciato attaccato
        scriverebbe il TTFT di questo turno sul prossimo."""
        for cls in (S.ChatSession, S.CodexChatSession, S.OpenCodeChatSession):
            with self.subTest(runtime=cls.__name__):
                src = inspect.getsource(cls.send_user_message)
                self.assertIn("self._timing = None", src)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
