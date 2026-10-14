"""clodia-platform#514 §3 · il messaggio del turno scaduto deve dire DOVE è
finito il budget, non solo che è finito.

Il gap è scritto nella issue con queste parole: «il timeout di 180s non
distingue *modello lento* da *gateway bloccato*». Fino a qui le due situazioni
arrivavano in chat con la stessa identica frase — tentativo, secondi, budget,
modello — e chi leggeva poteva solo andare a cercare il provider, che nelle
occorrenze del 7, dell'8 e del 12 ottobre risultava ogni volta connesso e senza
errori.

Due osservazioni che il processo aveva già e non diceva:

- **il conto degli eventi** del turno (il contatore che la §2 ha introdotto per
  decidere il rimando): zero eventi significa che dal runtime non è tornato
  niente, quindi nessuna tool call era partita e l'attesa sta a monte, fra
  opencode e il provider; un conto maggiore di zero significa che il turno
  stava lavorando, e lì il tempo può esserselo preso anche un tool appeso;
- **i guasti del gateway** caduti dentro la finestra del turno
  (`api/gateway_http.nota_gateway`), datati sull'orologio monotonico del
  budget: non «nelle ultime due ore» ma «durante QUESTO turno».

Nessuna delle due afferma una causa, e il verso dell'errore è deliberato: si
tace quando non si sa, invece di nominare il modello. È la stessa lezione per
cui la frase «modello X non convergente» è stata tolta dalla #423 — una
diagnosi scritta al posto di un'osservazione ha mandato due issue a inseguire il
provider mentre il difetto stava nel client.

Fuori scope, deciso sulla issue: il gateway `clodia-tools:7849` in sé (è di chi
amministra l'istanza), il fallback automatico su un altro modello, e lo stderr
del processo `opencode serve` — per quello non esiste strumento da questo lato.
"""
from __future__ import annotations

import asyncio
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import requests

from server.api import gateway_http as G
from . import session as S
from .test_423_budget_turno import APPESO, _500_SESSIONE_MORTA, _Serve, _sessione
from .test_514_budget_per_agente import _TurnoBase, _seed


def _gateway_giu(nome: str = "topics", volte: int = 1) -> None:
    """Fa cadere davvero `volte` chiamate al gateway, passando per il breaker
    invece di scrivere a mano nel registro: così il test misura il percorso che
    gira in produzione, non la propria idea di esso."""
    http = G.GatewayHTTP(nome, threshold=10 ** 6, cooldown=60.0)
    with mock.patch.object(requests, "request",
                           side_effect=requests.ConnectionError("read timed out")):
        for _ in range(volte):
            try:
                http.get("http://clodia-tools:7849/internal/topics")
            except requests.RequestException:
                pass


class LaNotaSugliEventiTests(unittest.TestCase):
    """Dove stava il turno quando il budget è finito."""

    def test_zero_eventi_dice_che_nessuna_tool_call_era_partita(self) -> None:
        nota = S._oc_nota_eventi(0)
        self.assertIn("nessun evento", nota)
        self.assertIn("a monte", nota)

    def test_con_eventi_dice_che_il_turno_stava_lavorando(self) -> None:
        nota = S._oc_nota_eventi(3)
        self.assertIn("3 eventi", nota)
        self.assertIn("tool call", nota)

    def test_senza_conto_si_tace(self) -> None:
        """`None` = «non so». Un «nessun evento» inventato sposterebbe la ricerca
        di chi legge, ed è precisamente l'errore che questo messaggio ha già
        pagato una volta."""
        self.assertEqual("", S._oc_nota_eventi(None))


class IlMessaggioPortaLeTreOsservazioniTests(unittest.TestCase):

    def _msg(self, **kw) -> str:
        base = dict(tentativo=S._OC_TENTATIVO_PRIMO, atteso=180.0, budget=180.0,
                    model="glm-5.2")
        return S._oc_turn_timeout_message(**{**base, **kw})

    def test_senza_niente_da_dire_il_messaggio_e_quello_di_prima(self) -> None:
        """Compatibilità: il messaggio non deve ingrassare quando non c'è altro
        da osservare, se no la coda diventa rumore attaccato a ogni timeout."""
        msg = self._msg()
        self.assertTrue(msg.endswith("turno interrotto"), msg)
        self.assertIn("attesi 180s sul budget di 180s", msg)
        self.assertIn("glm-5.2", msg)

    def test_il_gateway_giu_finisce_nel_messaggio(self) -> None:
        """Rosso prima: il fatto c'era (nel log del breaker) ma non arrivava a
        chi legge l'errore nella stanza."""
        msg = self._msg(gateway="nella stessa finestra il gateway non ha risposto 7 volte")
        self.assertIn("il gateway non ha risposto 7 volte", msg)

    def test_le_tre_code_stanno_in_ordine_di_vicinanza_al_turno(self) -> None:
        """Eventi (dentro il turno), gateway (finestra del turno), loop (poco
        prima del turno): chi legge scende dal fatto più vicino al più lontano."""
        msg = self._msg(eventi=0, gateway="GW", correlazione="LOOP")
        self.assertLess(msg.index("nessun evento"), msg.index("GW"))
        self.assertLess(msg.index("GW"), msg.index("LOOP"))

    def test_una_coda_vuota_non_lascia_un_trattino_a_vuoto(self) -> None:
        msg = self._msg(eventi=None, gateway="", correlazione="LOOP")
        self.assertNotIn("—  —", msg)
        self.assertTrue(msg.endswith("turno interrotto — LOOP"), msg)


class LaFinestraEIlTurnoTests(unittest.TestCase):

    def test_il_budget_espone_il_proprio_inizio(self) -> None:
        """Senza l'istante di inizio la correlazione tornerebbe a essere una
        stima («nelle ultime due ore»), cioè di nuovo un'euristica dove invece
        esiste un numero esatto."""
        falso = [100.0]
        b = S._TurnBudget(180.0, clock=lambda: falso[0])
        self.assertEqual(100.0, b.start)
        falso[0] = 130.0
        self.assertEqual(100.0, b.start, "l'inizio non deve muoversi col tempo")
        self.assertEqual(30.0, b.elapsed())


class IlTurnoScadutoDiceDoveEFinitoIlBudgetTests(_TurnoBase):
    """Il percorso vero: `_run_turn` contro l'`opencode serve` finto della #423."""

    def setUp(self) -> None:
        G.reset_guasti()
        self.addCleanup(G.reset_guasti)

    async def test_il_gateway_caduto_durante_il_turno_arriva_in_chat(self) -> None:
        """Rosso prima: il messaggio parlava solo di modello e secondi, e la
        finestra 08:50–09:00 del gateway restava da scoprire a mano nei log."""
        err, _s, _sess = await self._turno(
            [APPESO], seed=_seed(1),
            durante=lambda _sess: _gateway_giu("topics", volte=4))
        self.assertIn("il gateway non ha risposto 4 volte", str(err))
        self.assertIn("topics", str(err))

    async def test_un_gateway_caduto_prima_del_turno_non_ci_finisce(self) -> None:
        """La finestra è il turno. Un guasto di prima renderebbe ogni timeout
        successivo «colpa del gateway» finché il registro non si svuota."""
        _gateway_giu("topics", volte=5)
        await asyncio.sleep(0.05)
        err, _s, _sess = await self._turno([APPESO], seed=_seed(1))
        self.assertNotIn("il gateway non ha risposto", str(err))

    async def test_senza_guasti_il_messaggio_non_accusa_il_gateway(self) -> None:
        """Il ramo che deve restare muto: se fosse rosso anche questo, il verde
        del test sopra sarebbe per il motivo sbagliato."""
        err, _s, _sess = await self._turno([APPESO], seed=_seed(1))
        self.assertNotIn("gateway", str(err))
        self.assertIn("nessun evento dal runtime", str(err),
                      "il conto degli eventi deve esserci comunque")

    async def test_la_nota_c_e_anche_sul_tentativo_dopo_la_ricreazione(self) -> None:
        """A differenza della correlazione col loop — che vale solo lì — il
        gateway va su ENTRAMBI i tentativi: è un fatto datato dentro la finestra,
        non un'euristica sul passato recente, e il secondo tentativo è quello in
        cui il gateway giù è più probabile, non meno."""
        srv = _Serve([(0.3, 500, _500_SESSIONE_MORTA),   # sessione da rifare
                      (0.0, 200, '{"id":"oc-2"}'),       # ricreazione
                      APPESO])                           # il secondo invio non torna
        await srv.start()
        self.addAsyncCleanup(srv.stop)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sess = _sessione(srv.port)
        with mock.patch.object(S, "_OPENCODE_TURN_TIMEOUT", 2.0), \
             mock.patch.object(S, "_OPENCODE_MIN_RETRY_BUDGET", 0.2), \
             mock.patch.object(S, "_kind_spec", return_value=None), \
             mock.patch.object(S, "_resolve_sessions_dir",
                               return_value=pathlib.Path(d.name)), \
             mock.patch.object(S.activity_log, "append"):
            task = asyncio.ensure_future(sess._run_turn("riconcilia il foglio"))
            await asyncio.sleep(0.5)
            _gateway_giu("pdp", volte=2)
            with self.assertRaises(S.OpenCodeTurnTimeout) as ctx:
                await task
        self.assertIn(S._OC_TENTATIVO_RICREATA, str(ctx.exception))
        self.assertIn("il gateway non ha risposto 2 volte", str(ctx.exception))
        self.assertIn("pdp", str(ctx.exception))

    async def test_il_conto_dei_guasti_finisce_anche_nel_log_di_attivita(self) -> None:
        """La stanza vede una frase; chi indaga a posteriori ha bisogno di un
        numero su cui filtrare."""
        srv = _Serve([APPESO])
        await srv.start()
        self.addAsyncCleanup(srv.stop)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sess = _sessione(srv.port)
        with mock.patch.object(S, "_OPENCODE_TURN_TIMEOUT", 1.0), \
             mock.patch.object(S, "_kind_spec", return_value=None), \
             mock.patch.object(S, "_resolve_sessions_dir",
                               return_value=pathlib.Path(d.name)), \
             mock.patch.object(S.activity_log, "append") as log:
            task = asyncio.ensure_future(sess._run_turn("riconcilia il foglio"))
            await asyncio.sleep(0.2)
            _gateway_giu("topics", volte=3)
            with self.assertRaises(S.OpenCodeTurnTimeout):
                await task
        righe = [c.args[2] for c in log.call_args_list
                 if c.args[1] == "error" and c.args[2].get("error") == "opencode turn timeout"]
        self.assertEqual(1, len(righe), righe)
        self.assertEqual(3, righe[0]["gateway_giu"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
