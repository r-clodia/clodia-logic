"""Un turno che tagga @X in due bolle deve svegliare X una volta sola.

clodia-logic#434, punto 2 — la parte che la guardia su `trigger/internal` NON
copre. Quella ferma il turno REPLICATO (stesso testo, stesso chiamante, entro la
finestra). Qui il turno è uno solo e legittimo: è la delega a moltiplicarsi.

Da #243 una risposta è più messaggi — una bolla per blocco — e
`_run_and_post_response` serve la delega di OGNI bolla. Il dedup dei bersagli
però viveva dentro la singola chiamata a `_maybe_delegate` (`_distinct_by` sul
testo, la lista `started` locale), quindi `@X` nel primo blocco e `@X`
nell'ultimo erano due `_start_turn` su X: due spawn dello stesso seed che
ripartono dalla stessa storia del canale e possono rivendicare lo stesso lavoro
prima di potersi leggere a vicenda. È la seconda sorgente dei doppioni visti su
#420, #329 e #347, e non ha bisogno di nessun retrigger per prodursi.

La regola «un messaggio, un turno» resta vera per i messaggi di PERSONE: due
messaggi umani sono due richieste. Le bolle no: sono la stessa risposta dello
stesso agente nello stesso turno, spezzata per farla vedere prima.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from . import channels


class _Spec:
    """Il minimo che `_maybe_delegate` chiede a un partecipante."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.type = "bot"


class _Chat:
    """Sessione finta che consegna blocchi come farebbe `_collect_response`."""

    principal = ""

    def __init__(self, blocchi: list[str]) -> None:
        self.blocchi = list(blocchi)

    async def send_user_message(self, _prompt: str) -> str:
        cb = getattr(self, "on_visible_block", None)
        if cb is not None:
            for b in self.blocchi:
                await cb(b)
        return "\n\n".join(self.blocchi)


class UnaDelegaPerTurnoTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        self.messages: list[dict] = []
        self.turni: list[tuple[str, str]] = []   # (delegato, testo che lo ha svegliato)

        def post(_tier, _name, author, text, kind="human", **_kw):
            row = {"id": str(len(self.messages) + 1), "author": author,
                   "text": text, "kind": kind, "ts": str(len(self.messages) + 1)}
            self.messages.append(row)
            return row

        async def start_turn(_tier, _name, _tier_real, delegate, _principal,
                             testo, _kind, **_kw):
            self.turni.append((delegate.name, testo))
            return True

        async def open_topic(_tier, _name):
            return {"meta": {"tier": "P0",
                             "participants": ["clodia", "worker", "helper"],
                             "title": "ops"}}

        async def noop_async(*_a, **_kw):
            return None

        self._patches = [
            patch.object(channels.topics_client, "post_message", post),
            patch.object(channels.topics_client, "list_messages",
                         lambda *_a, **_kw: list(self.messages)),
            patch.object(channels.topics_client, "async_open_topic", open_topic),
            patch.object(channels, "_start_turn", start_turn),
            patch.object(channels, "_pick_responder",
                         lambda _p, _t, seed, *_a, **_kw: _Spec(seed) if seed else None),
            patch.object(channels, "_spec_of", lambda label: _Spec(label or "")),
            patch.object(channels, "_track_routing_decision", lambda _p: None),
            patch.object(channels, "_typing", noop_async),
            patch.object(channels, "_channel_message", noop_async),
            patch.object(channels, "_topic_title", lambda *_a, **_kw: None),
            patch.object(channels, "_spawn_bg", lambda _c: _c.close()),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    async def _run(self, blocchi: list[str]) -> None:
        with patch.dict(os.environ, {"CLODIA_BUBBLE_PER_BLOCK": "1"}):
            await channels._run_and_post_response(
                "P0", "ops", "clodia", _Chat(blocchi), "prompt")

    async def test_lo_stesso_bersaglio_in_due_bolle_e_un_turno_solo(self) -> None:
        """IL DIFETTO, nella sua forma più corta.

        Prima: due `_start_turn` su `worker` — due spawn che rivendicano lo
        stesso lavoro. Il primo `@` è la convocazione; il secondo, nello stesso
        turno dello stesso autore, è la stessa convocazione detta di nuovo.
        """
        await self._run(["@worker comincia tu", "ho finito, @worker chiudi"])
        self.assertEqual(["worker"], [n for n, _t in self.turni],
                         f"un solo turno per bersaglio, non {self.turni}")

    async def test_il_turno_lo_apre_la_prima_bolla_che_lo_chiede(self) -> None:
        """Vince la prima, non l'ultima: è l'ordine in cui l'autore ha scritto, e
        il delegato deve partire appena la richiesta compare — non alla fine del
        turno di chi delega."""
        await self._run(["@worker comincia tu", "ho finito, @worker chiudi"])
        self.assertEqual("@worker comincia tu", self.turni[0][1])

    async def test_bersagli_diversi_in_bolle_diverse_partono_entrambi(self) -> None:
        """La guardia è sul doppione, non sulla delega: due incarichi veri
        restano due turni."""
        await self._run(["@worker prendi la #434", "@helper tu guarda i log"])
        self.assertEqual({"worker", "helper"}, {n for n, _t in self.turni})

    async def test_un_turno_nuovo_puo_richiamare_lo_stesso_agente(self) -> None:
        """La memoria è del TURNO, non del canale. Richiamare @worker più tardi è
        una richiesta nuova: se questa memoria sopravvivesse al turno, un agente
        diventerebbe irraggiungibile dopo essere stato chiamato una volta."""
        await self._run(["@worker primo giro"])
        await self._run(["@worker secondo giro"])
        self.assertEqual(2, len(self.turni), f"turni: {self.turni}")


class DelegaSenzaMemoriaCondivisaTests(unittest.IsolatedAsyncioTestCase):
    """`_maybe_delegate` resta chiamabile da sola, senza il set del turno.

    Ha altri due chiamanti (la risposta non-a-bolle e il percorso di errore): il
    parametro è opzionale e l'assenza non deve cambiare il loro comportamento.
    """

    async def test_senza_il_set_ogni_chiamata_e_indipendente(self) -> None:
        turni: list[str] = []

        async def start_turn(_tier, _name, _tier_real, delegate, *_a, **_kw):
            turni.append(delegate.name)
            return True

        async def open_topic(_tier, _name):
            return {"meta": {"tier": "P0", "participants": ["clodia", "worker"]}}

        with patch.object(channels.topics_client, "async_open_topic", open_topic), \
             patch.object(channels, "_start_turn", start_turn), \
             patch.object(channels, "_pick_responder",
                          lambda _p, _t, seed, *_a, **_kw: _Spec(seed) if seed else None), \
             patch.object(channels, "_spec_of", lambda label: _Spec(label or "")), \
             patch.object(channels, "_track_routing_decision", lambda _p: None), \
             patch.object(channels, "_spawn_bg", lambda _c: _c.close()):
            await channels._maybe_delegate("P0", "ops", "clodia", "@worker vai", None, 0)
            await channels._maybe_delegate("P0", "ops", "clodia", "@worker vai", None, 0)

        self.assertEqual(["worker", "worker"], turni)


if __name__ == "__main__":
    unittest.main()
