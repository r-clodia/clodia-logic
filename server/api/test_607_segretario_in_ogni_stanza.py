"""Il coordinatore siede in ogni stanza, e i messaggi non indirizzati sono suoi.

clodia-platform#607 (R3 dell'epic #604). Due fatti, e il secondo dipende dal
primo: il segretario coordina la stanza, ma `_pick_responder` sceglie fra i
PARTECIPANTI — in una stanza dove non è stato invitato non esiste, e il ripiego
cade sul rango, cioè su un agente che nessuno ha designato a coordinare.

L'auto-partecipazione è il gemello di `reconcile_membership`, che è l'unico
punto che TOGLIE qualcuno da una stanza: le due funzioni si guardano, e la
condizione di idoneità dev'essere la stessa o si innesca un ping-pong —
l'apertura del canale lo aggiunge, la riconciliazione lo espelle, ogni volta
con la sua riga in chat.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from . import channels
from ..agents import coordinator as coordinator_mod


def _spec(name: str, **kw):
    base = {"name": name, "type": "bot", "providers": ["claude-team"],
            "provider": None, "agent_sdk": "claude", "model": "claude-sonnet-5-5",
            "provider_models": {}, "all_tier": False, "routing_mode": "normal",
            "rank": 0, "created_at": None}
    base.update(kw)
    return SimpleNamespace(**base)


#: Lo stack vero del segretario dopo #607, nella forma derivata: Sonnet sui
#: provider Anthropic, gemma su Scaleway (l'unico SEAL-3 del catalogo).
SEGRETARIO = _spec(
    "segretario", all_tier=True,
    providers=["claude-team", "claude-pro-max", "anthropic-api",
               "aws-region-eu", "scaleway"],
    provider_models={"scaleway": "gemma-4-26b-a4b-it"})


class ChiSiedeInOgniStanzaTests(unittest.TestCase):
    """La decisione pura: chi manca, e quando non si aggiunge."""

    def setUp(self) -> None:
        self._orig = channels.registry.get_by_name
        channels.registry.get_by_name = lambda n: {"segretario": SEGRETARIO}.get(n)

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig

    def test_e_l_ultimo_coordinatore_dichiarato(self) -> None:
        """Non un nome scritto a mano: l'ULTIMO di `coordinator.DECLARED`.

        È quello che non ha nessuno dietro di sé — la rete della stanza. Scritto
        così, #608 (Clodia esce dal coordinamento) accorcia una riga sola e
        questa funzione continua a dire la cosa giusta.
        """
        meta = {"tier": "SEAL-1", "participants": ["owner", "worker"]}
        self.assertEqual(coordinator_mod.DECLARED[-1],
                         channels._auto_participant_missing(meta, "SEAL-1"))

    def test_non_lo_riaggiunge_se_c_e_gia(self) -> None:
        meta = {"tier": "SEAL-1", "participants": ["owner", "segretario"]}
        self.assertIsNone(channels._auto_participant_missing(meta, "SEAL-1"))

    def test_riconosce_anche_la_mappa_dei_ruoli(self) -> None:
        """`participants` è una lista legacy o una mappa nome→ruolo: leggere
        solo la prima forma lo farebbe riaggiungere a ogni apertura."""
        meta = {"tier": "SEAL-1",
                "participants": {"owner": "owner", "segretario": "contributor"}}
        self.assertIsNone(channels._auto_participant_missing(meta, "SEAL-1"))

    def test_non_entra_dove_la_riconciliazione_lo_caccerebbe(self) -> None:
        """La condizione è la stessa di `reconcile_membership`, al contrario.

        Senza questo ramo una stanza di tier superiore allo stack dichiarato
        diventa un ping-pong: aggiunto all'apertura, espulso dalla
        riconciliazione, con l'annuncio di uscita a ogni giro.
        """
        meta = {"tier": "SEAL-4", "participants": ["owner"]}
        self.assertIsNone(channels._auto_participant_missing(meta, "SEAL-4"))
        # e la prova che le due funzioni parlano dello stesso fatto:
        self.assertTrue(channels._declared_below_tier(SEGRETARIO, "SEAL-4"))
        self.assertFalse(channels._declared_below_tier(SEGRETARIO, "SEAL-3"))

    def test_una_stanza_senza_meta_non_e_una_stanza(self) -> None:
        self.assertIsNone(channels._auto_participant_missing({}, None))


class LAggiuntaEIdempotenteTests(unittest.TestCase):
    """Il wrapper che scrive: una volta sola, e muta il `meta` in mano."""

    def setUp(self) -> None:
        self._orig = channels.registry.get_by_name
        channels.registry.get_by_name = lambda n: {"segretario": SEGRETARIO}.get(n)

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig

    def test_scrive_e_aggiorna_la_lista_in_mano(self) -> None:
        meta = {"tier": "SEAL-1", "participants": ["owner", "worker"]}
        with patch.object(channels.topics_client, "set_participant") as sp, \
                patch.object(channels.activity_log, "append", lambda *a, **k: None):
            agg = channels.ensure_auto_participant("SEAL-1", "ops", meta)
        self.assertEqual("segretario", agg)
        sp.assert_called_once_with("SEAL-1", "ops", "segretario", add=True)
        # il chiamante usa SUBITO `meta["participants"]` per instradare: se la
        # lista non si aggiorna, il turno in corso non lo vede ancora.
        self.assertIn("segretario", meta["participants"])
        # seconda apertura: niente da fare, e nessuna seconda scrittura
        with patch.object(channels.topics_client, "set_participant") as sp2:
            self.assertIsNone(channels.ensure_auto_participant("SEAL-1", "ops", meta))
        sp2.assert_not_called()

    def test_un_gateway_muto_non_sporca_la_lista(self) -> None:
        """Se la scrittura fallisce, `meta` non deve dire che è dentro: il turno
        lo instraderebbe su un agente che la stanza non ha."""
        meta = {"tier": "SEAL-1", "participants": ["owner"]}
        with patch.object(channels.topics_client, "set_participant",
                          side_effect=RuntimeError("gateway giù")):
            self.assertIsNone(channels.ensure_auto_participant("SEAL-1", "ops", meta))
        self.assertNotIn("segretario", meta["participants"])


class ILMessaggioNonIndirizzatoTests(unittest.IsolatedAsyncioTestCase):
    """Il fatto che l'auto-partecipazione esiste per produrre."""

    async def test_in_una_stanza_senza_coordinatori_il_turno_e_suo(self) -> None:
        """Nessun `@`, e nella stanza c'è solo uno specialista: senza la seduta
        automatica risponderebbe lui per rango — cioè chi nessuno ha designato.
        """
        agents = {"worker": _spec("worker"), "segretario": SEGRETARIO,
                  # una persona vera: senza, R21 scarta il messaggio come
                  # chiacchiera di bot e nessun turno parte.
                  "owner": _spec("owner", type="human")}
        meta = {"tier": "SEAL-1", "owner": "owner",
                "participants": ["owner", "worker"]}
        start = AsyncMock(return_value=True)
        with patch.object(channels.registry, "get_by_name", lambda n: agents.get(n)), \
                patch.object(channels, "_provider_seal_ok", return_value=True), \
                patch.object(channels.topics_client, "open_topic",
                             return_value={"meta": meta}), \
                patch.object(channels.topics_client, "set_participant") as sp, \
                patch.object(channels.topics_client, "post_message",
                             return_value={"id": "m1"}), \
                patch.object(channels.topics_client, "list_messages", return_value=[]), \
                patch.object(channels.access_log, "touch", lambda *a, **k: None), \
                patch.object(channels.activity_log, "append", lambda *a, **k: None), \
                patch.object(channels, "_channel_message", AsyncMock()), \
                patch.object(channels, "_track_routing_decision", lambda _p: None), \
                patch.object(channels, "_start_turn", start):
            await channels.post_channel_message("SEAL-1", "ops",
                                                "qualcuno mi aiuta?", "owner")
        sp.assert_called_once_with("SEAL-1", "ops", "segretario", add=True)
        avviati = [c.args[3].name for c in start.await_args_list]
        self.assertEqual(["segretario"], avviati)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
