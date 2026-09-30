"""clodia-platform#423 · «entro 180s» deve essere vero, e il messaggio deve dire cosa si è visto.

L'incidente: due turni di `segretario` falliti con

    RuntimeError('turno opencode non concluso entro 180s
                  (modello gemma-4-26b-a4b-it non convergente) — turno interrotto')

Due cose sbagliate in quella riga.

**Il numero.** `_http()` costruisce `httpx.Timeout(_OPENCODE_TURN_TIMEOUT)`, che
in httpx è **per-richiesta**; `_run_turn` ne emette fino a tre — `/message`,
poi `/session`, poi di nuovo `/message` quando la sessione va ricreata. Ogni
richiesta ripartiva da 180s, quindi un turno annunciato «non concluso entro
180s» poteva averne attesi fino a ~540. Il log dell'incidente lo dice da sé:
«→ ricreo» alle 09:11:09 e il fallimento alle 09:14:09, cioè 180s **pieni dopo**
la ricreazione. Il watchdog di turno, tarato su quella soglia, chiudeva la
sessione nel frattempo — e chi leggeva il log non aveva modo di saperlo.

**La frase.** «modello X non convergente» non è un'osservazione, è una
diagnosi: su due occorrenze è sbagliata almeno una volta, ed è quella frase ad
aver mandato due issue (#67 e questa) a inseguire il provider mentre il difetto
stava nel client. Ora si dichiarano il tentativo scaduto e i secondi realmente
attesi.

NON è in scope: la soglia dei 180s e il watchdog (#358 documenta che è
misurata), `_session_unusable` (ricreare la sessione una volta per turno resta
giusto) e il trace-id condiviso fra agent-server e gateway files — quello è
l'altro gap della issue e vive a cavallo di due componenti.

Nota sull'altra ipotesi della issue, refutata leggendo il codice e non misurata
qui: gli HTTP 500 del gateway files finiscono in `except` che loggano e tornano
`None` (`channels._files_hint`, lettore trifecta). Degradano il preambolo, non
trattengono il turno: erano rumore concomitante, non la causa.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import re
import tempfile
import time
import unittest
from collections import deque
from datetime import datetime, timezone
from unittest import mock

from . import session as S

#: Corpo di un 500 che `_session_unusable` riconosce come «sessione da rifare».
_500_SESSIONE_MORTA = '{"data":{"name":"UnknownError","message":"unexpected"}}'
#: Nello script del server finto: «non rispondere mai» (il turno appeso).
APPESO = None


class _Serve:
    """Un `opencode serve` finto e scriptabile: risponde, tarda, o resta appeso.

    Serve un server vero e non un mock di httpx perché la cosa da misurare è
    proprio **quanto si aspetta**: con `httpx` finto il timeout non esiste e il
    difetto non si vede.
    """

    def __init__(self, script) -> None:
        #: ogni voce: `(ritardo, status, body)` oppure `APPESO`.
        self.script = list(script)
        self.paths: list[str] = []
        self._srv = None
        self._stop = asyncio.Event()
        self.port = 0

    async def start(self) -> None:
        self._srv = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._srv.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._stop.set()
        self._srv.close()
        try:
            await self._srv.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    async def _handle(self, reader, writer) -> None:
        try:
            testa = await reader.readuntil(b"\r\n\r\n")
        except Exception:  # noqa: BLE001
            writer.close()
            return
        righe = testa.decode("latin-1").split("\r\n")
        parti = righe[0].split(" ")
        self.paths.append(parti[1] if len(parti) > 1 else "?")
        for riga in righe[1:]:
            if riga.lower().startswith("content-length:"):
                try:
                    await reader.readexactly(int(riga.split(":", 1)[1]))
                except Exception:  # noqa: BLE001
                    pass
        azione = self.script.pop(0) if self.script else (0.0, 200, "{}")
        if azione is APPESO:
            await self._stop.wait()       # il turno che non torna mai
            writer.close()
            return
        ritardo, status, body = azione
        if ritardo:
            await asyncio.sleep(ritardo)
        dati = body.encode()
        writer.write(b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\n"
                     b"Content-Length: %d\r\nConnection: close\r\n\r\n"
                     % (status, len(dati)) + dati)
        try:
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        writer.close()


def _sessione(port: int) -> S.OpenCodeChatSession:
    s = S.OpenCodeChatSession.__new__(S.OpenCodeChatSession)
    s.kind = "segretario"
    s.chat_id = "chan-SEAL-1-participation-exit-davide-segretario"
    s.principal = "owner"
    s._base_url = f"http://127.0.0.1:{port}"
    s._oc_session = "oc-1"
    s._provider = "scaleway"
    s._model = "gemma-4-26b-a4b-it"
    s._stderr_tail = deque(maxlen=20)
    s._timing = None
    s._last_usage = {}
    s._runtime_override = None
    s.last_activity = datetime.now(timezone.utc)
    return s


class _TurnoBase(unittest.IsolatedAsyncioTestCase):

    async def _turno(self, script, *, budget: float, minimo: float = 20.0):
        """Esegue un `_run_turn` contro il server finto. Ritorna (errore, secondi, server)."""
        srv = _Serve(script)
        await srv.start()
        self.addAsyncCleanup(srv.stop)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sess = _sessione(srv.port)
        t0 = asyncio.get_running_loop().time()
        with mock.patch.object(S, "_OPENCODE_TURN_TIMEOUT", budget), \
             mock.patch.object(S, "_OPENCODE_MIN_RETRY_BUDGET", minimo), \
             mock.patch.object(S, "_resolve_sessions_dir",
                               return_value=pathlib.Path(d.name)), \
             mock.patch.object(S.activity_log, "append"):
            with self.assertRaises(RuntimeError) as ctx:
                await sess._run_turn("verifica i 12 allegati e archiviali")
        return ctx.exception, asyncio.get_running_loop().time() - t0, srv


class UnBudgetPerTurnoTests(_TurnoBase):
    """Il turno intero sta nel budget, anche quando la sessione va ricreata."""

    async def test_il_retry_eredita_il_residuo_e_non_un_budget_nuovo(self) -> None:
        """Rosso prima: 1,5s sul primo invio + 3s PIENI sul secondo ≈ 4,5s, cioè
        un turno «entro 3s» durato una volta e mezza il suo budget.

        Nello stesso giro si misura anche il messaggio: dichiara i secondi
        realmente attesi (quelli del TURNO, non quelli dell'ultima richiesta) e
        quale dei due invii è scaduto.
        """
        err, secondi, _srv = await self._turno(
            [(1.5, 500, _500_SESSIONE_MORTA),        # sessione da rifare, dopo 1,5s
             (0.0, 200, '{"id":"oc-2"}'),            # ricreazione
             APPESO],                                # il secondo invio non torna
            budget=3.0, minimo=0.5)
        self.assertLess(secondi, 3.9,
                        f"il turno ha sforato il budget di 3s: {secondi:.1f}s — {err}")
        self.assertIn(S._OC_TENTATIVO_RICREATA, str(err))
        m = re.search(r"attesi (\d+)s", str(err))
        self.assertIsNotNone(m, f"il messaggio non dichiara l'attesa: {err}")
        self.assertGreaterEqual(int(m.group(1)), 2, f"attesa sottostimata: {err}")


class IlMessaggioDiceCosaSiEVistoTests(_TurnoBase):

    async def test_non_accusa_il_modello(self) -> None:
        """Rosso prima: «modello ... non convergente», che è una conclusione."""
        err, _s, _srv = await self._turno([APPESO], budget=1.0)
        self.assertNotIn("non convergente", str(err))
        self.assertIn("gemma-4-26b-a4b-it", str(err), "il modello resta citato come dato")

    async def test_dice_quale_tentativo_e_scaduto(self) -> None:
        err, _s, _srv = await self._turno([APPESO], budget=1.0)
        self.assertIn(S._OC_TENTATIVO_PRIMO, str(err))
        self.assertNotIn(S._OC_TENTATIVO_RICREATA, str(err))


class SenzaBudgetNonSiRiprovaTests(_TurnoBase):

    async def test_fail_fast_invece_di_bruciare_il_residuo(self) -> None:
        """Rosso prima: la sessione veniva ricreata comunque e il secondo invio
        partiva con 0,2s utili — un timeout in più e nessuna risposta in chat."""
        err, secondi, srv = await self._turno(
            [(0.8, 500, _500_SESSIONE_MORTA), (0.0, 200, '{"id":"oc-2"}'), APPESO],
            budget=1.0, minimo=20.0)
        self.assertEqual([p for p in srv.paths if p == "/session"], [],
                         "la sessione è stata ricreata senza budget per usarla")
        self.assertLess(secondi, 1.0 + 0.6, f"{secondi:.1f}s")
        self.assertIn("sotto il minimo", str(err))


class OgniRichiestaStaNelResiduoTests(unittest.TestCase):
    """Guardia statica: una POST nuova in `_run_turn` senza `timeout=` ricrea il
    difetto in silenzio, perché erediterebbe il default del client (il budget
    INTERO). Il caso è una classe di chiamate, non una chiamata."""

    def test_tutte_le_post_del_turno_passano_un_timeout(self) -> None:
        sorgente = pathlib.Path(S.__file__).read_text(encoding="utf-8")
        albero = ast.parse(sorgente)
        fn = next(n for n in ast.walk(albero)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_turn"
                  and any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "post"
                          for d in ast.walk(n)))
        post = [n for n in ast.walk(fn)
                if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "post"]
        self.assertGreaterEqual(len(post), 3, "le tre POST del turno non ci sono più")
        senza = [n.lineno for n in post
                 if not any(k.arg == "timeout" for k in n.keywords)]
        self.assertEqual(senza, [], f"POST senza timeout residuo alle righe {senza}")


class IlTtlDelGrantFirmatoTests(unittest.TestCase):
    """`import time` è portante per DUE cose, non solo per `_TurnBudget`.

    Trovato in review su questa PR: `_runtime_token_ttl` (riga ~1112) chiama
    `time.time()` da sempre, ma il modulo non importava `time` — quindi su
    `main` quella riga solleva `NameError`, non un TTL. Verificato eseguendo la
    funzione estratta dalla versione di `main`: `NameError: name 'time' is not
    defined` sul ramo con `expires_at`, e `86400` sul ramo senza. Ecco perché
    nessuno se n'era accorto: il difetto vive **solo** sul percorso in cui un
    overlay ha una scadenza, cioè quando un grant firmato andrebbe accorciato —
    il caso in cui sbagliare costa di più.

    Il mio `import time` lo ripara per effetto collaterale, e un fix per
    effetto collaterale è un fix che il prossimo cleanup può togliere: qui c'è
    il presidio. Complementare al controllo statico di
    `test_clearance_follows_spawn.WiringTests`, che verifica *dove* il TTL è
    usato ma non che sappia calcolarlo.
    """

    def test_un_overlay_con_scadenza_accorcia_il_ttl(self) -> None:
        """Rosso su `main` (`NameError`), verde con l'import."""
        ttl = S._runtime_token_ttl({"records": [{"expires_at": time.time() + 60}]})
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, 60)
        self.assertLess(ttl, S._CLODIA_TOOLS_TOKEN_TTL,
                        "la scadenza dell'overlay non ha accorciato niente")

    def test_senza_scadenze_resta_il_ttl_pieno(self) -> None:
        """Il ramo che su `main` funzionava: se passasse solo questo, il test
        sopra sarebbe verde per il motivo sbagliato."""
        self.assertEqual(S._runtime_token_ttl({"records": [{}]}),
                         S._CLODIA_TOOLS_TOKEN_TTL)
        self.assertEqual(S._runtime_token_ttl(None), S._CLODIA_TOOLS_TOKEN_TTL)

    def test_limport_resta_anche_se_TurnBudget_sparisce(self) -> None:
        """Il presidio deve sopravvivere al cleanup di #423.

        Oggi togliere `import time` rompe l'import del modulo, perché
        `_TurnBudget.__init__` lo usa come default: un rosso rumoroso ma che
        punta al posto sbagliato. Se un giorno `_TurnBudget` andrà via, questo
        controllo resta e nomina il vero motivo per cui l'import serve.
        """
        albero = ast.parse(pathlib.Path(S.__file__).read_text(encoding="utf-8"))
        moduli = {a.name for n in albero.body if isinstance(n, ast.Import)
                  for a in n.names}
        self.assertIn("time", moduli,
                      "`_runtime_token_ttl` chiama time.time(): senza import è NameError")
        fn = next(n for n in albero.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_runtime_token_ttl")
        self.assertIn("time", {getattr(d.func.value, "id", "") for d in ast.walk(fn)
                               if isinstance(d, ast.Call)
                               and isinstance(d.func, ast.Attribute)},
                      "il motivo di questo presidio non è più in quella funzione")

    def test_una_scadenza_gia_passata_non_conia_un_ttl_non_positivo(self) -> None:
        """Stesso ramo, aritmetica opposta: un TTL ≤ 0 firmerebbe un grant già
        morto invece di fallire subito."""
        self.assertEqual(S._runtime_token_ttl(
            {"records": [{"expires_at": time.time() - 3600}]}), 1)


class IlBudgetDelTurnoTests(unittest.TestCase):
    """`_TurnBudget` da solo: il residuo scende e non va sotto zero."""

    def test_il_residuo_scende_col_tempo_trascorso(self) -> None:
        ora = [100.0]
        b = S._TurnBudget(180.0, clock=lambda: ora[0])
        self.assertEqual(b.remaining(), 180.0)
        ora[0] += 120.0
        self.assertEqual(b.elapsed(), 120.0)
        self.assertEqual(b.remaining(), 60.0)

    def test_il_residuo_non_e_mai_negativo(self) -> None:
        ora = [0.0]
        b = S._TurnBudget(10.0, clock=lambda: ora[0])
        ora[0] += 999.0
        self.assertEqual(b.remaining(), 0.0)
        self.assertEqual(b.elapsed(), 999.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
