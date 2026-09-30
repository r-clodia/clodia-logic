"""Un turno e le chiamate al gateway files portano lo STESSO nome
(clodia-platform#455).

Il difetto che questi test misurano non è una risposta sbagliata: è che un 500
del gateway files e il turno che lo ha provocato lasciano due tracce
indipendenti nello stesso intervallo, e correlarle richiede archeologia dei log.
Quindi si verifica esattamente questo: che l'identificatore esista, che viaggi
sul filo, che sopravviva ai due salti veri del percorso (il thread del
preambolo e il task del turno) e che il 500 lasci UNA riga in cui si legge.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from ..core import trace, turn_timing
from . import channels, gateway_http, topics_client


class _Resp:
    def __init__(self, status: int = 200, payload: dict | None = None):
        self.status_code = status
        self._payload = payload if payload is not None else {"files": []}
        self.text = "boom" if status >= 500 else ""

    def json(self):
        return self._payload


class _Spia:
    """Sostituisce `requests.request` e conserva gli header di ogni chiamata."""

    def __init__(self, status: int = 200):
        self.status = status
        self.headers: list[dict] = []

    def __call__(self, method, url, **kwargs):
        self.headers.append(dict(kwargs.get("headers") or {}))
        return _Resp(self.status)

    @property
    def ultimo_trace(self) -> str | None:
        return (self.headers[-1] if self.headers else {}).get(trace.HEADER)


class TraceCorrenteTests(unittest.TestCase):
    def setUp(self):
        trace.bind(None)
        self.addCleanup(trace.bind, None)

    def test_il_cronometro_del_turno_conia_e_lega_il_trace(self):
        """Il trace nasce dove nasce il turno: nel dispatcher, non nella
        sessione. Le chiamate files del preambolo girano PRIMA che il turno
        esista, e un id coniato più a valle non le etichetterebbe mai."""
        self.assertIsNone(trace.current())
        t = turn_timing.begin("start_turn")
        self.assertEqual(trace.current(), t.trace)
        self.assertEqual(len(t.trace), 32)
        int(t.trace, 16)  # hex puro, formato W3C

    def test_due_turni_hanno_due_nomi(self):
        a = turn_timing.begin("start_turn")
        b = turn_timing.begin("topic_turn")
        self.assertNotEqual(a.trace, b.trace)

    def test_la_riga_ttft_riporta_il_trace(self):
        t = turn_timing.begin("start_turn")
        t.bind("chat:1")
        self.assertIn(f"trace={t.trace}", t.line())

    def test_la_sessione_riprende_il_trace_del_turno_ritirato(self):
        """`bind`/`claim` è la consegna dal dispatcher alla sessione: è lì che
        il turno cambia task, e il trace deve attraversare quel salto anche
        quando il contesto non è stato ereditato."""
        t = turn_timing.begin("start_turn")
        t.bind("chat:2")
        trace.bind(None)  # come un task nato fuori dal contesto del dispatcher
        turn_timing.adopt(turn_timing.claim("chat:2"))
        self.assertEqual(trace.current(), t.trace)

    def test_adopt_senza_cronometro_non_inventa_un_trace(self):
        trace.bind(None)
        turn_timing.adopt(None)
        self.assertIsNone(trace.current())


class DispatcherAnnidatiTests(unittest.TestCase):
    """Un dispatcher avvia il turno di un altro agente mentre il primo gira
    (delega): il nome del secondo non deve restare addosso al primo."""

    def setUp(self):
        trace.bind(None)
        self.addCleanup(trace.bind, None)

    def test_i_due_dispatcher_restituiscono_il_trace_al_chiamante(self):
        for dispatcher in (channels._start_turn, channels.run_topic_turn):
            with self.subTest(dispatcher=dispatcher.__name__):
                self.assertTrue(
                    getattr(dispatcher, "__wrapped__", None) is not None,
                    f"{dispatcher.__name__} non restituisce il trace al chiamante")

    def test_il_turno_delegato_non_ruba_il_nome_al_delegante(self):
        async def delegato():
            turn_timing.begin("start_turn")  # come un dispatcher annidato

        @trace.own_turn
        async def dispatcher():
            interno = turn_timing.begin("start_turn").trace
            await delegato()
            return interno

        async def scenario():
            delegante = turn_timing.begin("topic_turn").trace
            interno = await dispatcher()
            return delegante, interno, trace.current()

        delegante, interno, dopo = asyncio.run(scenario())
        self.assertNotEqual(delegante, interno)
        self.assertEqual(dopo, delegante)

    def test_il_task_lasciato_dal_dispatcher_tiene_il_suo_trace(self):
        """Il ripristino non deve togliere il nome al turno appena avviato: il
        task lo ha già copiato dal contesto."""
        visto: list[str | None] = []

        @trace.own_turn
        async def dispatcher():
            t = turn_timing.begin("start_turn")

            async def turno():
                visto.append(trace.current())

            return t.trace, asyncio.create_task(turno())

        async def scenario():
            atteso, task = await dispatcher()
            await task
            return atteso

        atteso = asyncio.run(scenario())
        self.assertEqual(visto, [atteso])


class HeaderVersoIlGatewayTests(unittest.TestCase):
    def setUp(self):
        trace.bind(None)
        self.addCleanup(trace.bind, None)

    def test_header_presente_quando_un_turno_e_in_corso(self):
        spia = _Spia()
        t = turn_timing.begin("start_turn")
        with patch.object(gateway_http.requests, "request", spia):
            gateway_http.GatewayHTTP("topics").get(
                "http://gw/internal/topics/SEAL-1/x/files",
                headers={"Authorization": "Bearer ckt1"})
        self.assertEqual(spia.ultimo_trace, t.trace)
        # l'header del chiamante resta: si aggiunge, non si sostituisce
        self.assertEqual(spia.headers[-1].get("Authorization"), "Bearer ckt1")

    def test_nessun_header_fuori_da_un_turno(self):
        """Una chiamata della webui non appartiene a nessun turno: etichettarla
        con l'id di un turno passato sarebbe una correlazione inventata."""
        spia = _Spia()
        with patch.object(gateway_http.requests, "request", spia):
            gateway_http.GatewayHTTP("topics").get("http://gw/internal/topics")
        self.assertIsNone(spia.ultimo_trace)

    def test_il_trace_arriva_al_gateway_files_dal_preambolo(self):
        """Il percorso vero del files-hint: `asyncio.to_thread` +
        `topics_client.list_files`. È il salto in cui un id tenuto in una
        variabile del dispatcher si perderebbe."""
        spia = _Spia()

        async def scenario():
            t = turn_timing.begin("start_turn")
            with patch.object(gateway_http.requests, "request", spia), \
                    patch.object(topics_client, "_headers", lambda: {}):
                await asyncio.to_thread(topics_client.list_files, "SEAL-1", "x", "")
            return t.trace

        atteso = asyncio.run(scenario())
        self.assertEqual(spia.ultimo_trace, atteso)

    def test_il_trace_del_turno_e_quello_delle_chiamate_files_coincidono(self):
        """La proprietà per cui esiste la issue: turno e chiamata portano lo
        stesso nome, quindi un 500 concomitante è attribuibile."""
        spia = _Spia()

        async def scenario():
            t = turn_timing.begin("topic_turn")
            t.bind("chat:3")
            with patch.object(gateway_http.requests, "request", spia), \
                    patch.object(topics_client, "_headers", lambda: {}):
                await asyncio.to_thread(topics_client.list_files, "SEAL-1", "x", "")
            # la sessione ritira il turno: stesso nome, dall'altro lato
            turn_timing.adopt(turn_timing.claim("chat:3"))
            return t.trace, trace.current()

        atteso, in_sessione = asyncio.run(scenario())
        self.assertEqual(spia.ultimo_trace, atteso)
        self.assertEqual(in_sessione, atteso)


class CinqueCentoNeiLogTests(unittest.TestCase):
    def setUp(self):
        trace.bind(None)
        self.addCleanup(trace.bind, None)

    def test_un_500_lascia_una_riga_con_il_trace(self):
        """Oggi il 500 del gateway files non lascia NESSUNA riga da questo lato:
        l'eccezione è inghiottita dal chiamante del preambolo, che logga solo
        «elenco non disponibile». Senza una riga, non c'è niente da correlare."""
        spia = _Spia(status=500)
        t = turn_timing.begin("start_turn")
        with patch.object(gateway_http.requests, "request", spia):
            with self.assertLogs("agent-server.gateway_http", level="WARNING") as log:
                gateway_http.GatewayHTTP("topics").get(
                    "http://gw/internal/topics/SEAL-1/x/files?path=")
        righe = [r for r in log.output if t.trace in r]
        self.assertEqual(len(righe), 1, log.output)
        self.assertIn("500", righe[0])
        self.assertIn("/internal/topics/SEAL-1/x/files", righe[0])

    def test_una_risposta_buona_non_logga(self):
        spia = _Spia(status=200)
        turn_timing.begin("start_turn")
        with patch.object(gateway_http.requests, "request", spia):
            with patch.object(gateway_http.LOG, "warning") as w:
                gateway_http.GatewayHTTP("topics").get("http://gw/internal/topics")
        w.assert_not_called()


if __name__ == "__main__":
    unittest.main()
