"""Una stanza che cambia tier cambia coordinatore, e adesso lo dice.

Residuo A4 di clodia-platform#195, staccato come clodia-platform#345.

Il *provider* di una sessione viva viene già ricalcolato quando il tier della
stanza sale (`_provider_della_stanza_ancora_valido`, clodia-platform#305). Il
COORDINAMENTO no: l'idoneità si ricalcola a ogni turno, quindi se clodia non
regge il nuovo tier dal turno dopo coordina `segretario` — che è corretto — ma
l'unica traccia era la `reason` del singolo messaggio
(`fallback-coordinatore dichiarato (segretario)`). Chi apre la trace lo vede,
chi legge la chat no. È lo stesso difetto che #195 chiamava «silence is the one
option that must go», sopravvissuto sul percorso del cambio di tier a stanza
aperta invece che su quello della configurazione.

Le quattro asserzioni che contano, e perché non basta la prima:

1. la prima volta che si vede una stanza non c'è nessuna transizione da
   raccontare — annunciare lì sarebbe rumore a ogni riavvio del processo;
2. alla promozione l'annuncio esce UNA volta;
3. e non esce di nuovo ai turni successivi: l'idoneità si ricalcola sempre, il
   racconto no. Questa è l'asserzione che un'implementazione ingenua sbaglia;
4. il verso opposto (tier abbassato, clodia torna a coordinare) è la stessa
   transizione letta al contrario, e va detto anche quello.

Più le due che tengono il codice attaccato ai suoi chiamanti: i punti che
avviano un turno di canale sono DUE (`_start_turn` e `run_topic_turn`), e
coprirne uno solo ripeterebbe il difetto che il codice accanto già denuncia.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from . import channels

TIER = "SEAL-1"
STANZA = "software-house"
PARTECIPANTI = ["davide", "clodia", "segretario", "avvocato"]

_MAX = {"clodia": 2, "segretario": 4, "avvocato": 2}


class _Spec:
    def __init__(self, name: str):
        self.name = name
        self.type = "bot"
        self.clearance = f"SEAL-{_MAX.get(name, 0)}"


def _get_by_name(nome: str):
    return _Spec(nome) if nome in _MAX else None


def _seal_ok(spec, tier: str | None) -> bool:
    """Il provider dell'agente regge il tier? Qui: clodia fino a SEAL-2,
    segretario ovunque — cioè esattamente il caso A4 dell'agents-notebook."""
    livello = int(str(tier or "SEAL-0").split("-")[1])
    return livello <= _MAX.get(spec.name, 0)


def _esegui(coro):
    return asyncio.run(coro)


class _Stanza:
    """La stanza vista dagli annunci: cosa è stato scritto, e quante volte."""

    def __init__(self):
        self.messaggi: list[str] = []
        self.eventi: list[tuple] = []

    async def _post(self, tier, name, author, text, **kw):
        self.messaggi.append(text)
        return {"id": f"m{len(self.messaggi)}", "text": text}

    async def _channel_message(self, *a, **kw):
        return None

    def _log(self, agent, event_type, payload, **kw):
        self.eventi.append((agent, event_type, payload))

    def patch(self):
        return (
            patch.object(channels.topics_client, "async_post_message", self._post),
            patch.object(channels, "_channel_message", self._channel_message),
            patch.object(channels.activity_log, "append", self._log),
            patch.object(channels.registry, "get_by_name", _get_by_name),
            patch.object(channels, "_provider_seal_ok", _seal_ok),
        )


class CambioCoordinatoreTests(unittest.TestCase):
    def setUp(self) -> None:
        channels._tier_visto_reset()
        self.stanza = _Stanza()
        self._patches = self.stanza.patch()
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def _turno(self, tier_real: str) -> None:
        """Un turno di canale in questa stanza, al tier dato."""
        _esegui(channels._annuncia_cambio_coordinatore(
            TIER, STANZA, tier_real, PARTECIPANTI))

    def test_la_prima_volta_che_si_vede_la_stanza_non_si_annuncia_nulla(self) -> None:
        self._turno("SEAL-2")
        self.assertEqual(self.stanza.messaggi, [])

    def test_la_promozione_di_tier_annuncia_il_passaggio_una_volta_sola(self) -> None:
        self._turno("SEAL-2")
        self._turno("SEAL-4")
        self.assertEqual(len(self.stanza.messaggi), 1, self.stanza.messaggi)
        testo = self.stanza.messaggi[0]
        self.assertIn("clodia", testo)
        self.assertIn("segretario", testo)
        self.assertIn("SEAL-4", testo)
        self.assertEqual(
            [(a, t) for a, t, _p in self.stanza.eventi],
            [("segretario", "coordinator_changed")],
        )

    def test_i_turni_successivi_non_lo_ripetono(self) -> None:
        self._turno("SEAL-2")
        self._turno("SEAL-4")
        self._turno("SEAL-4")
        self._turno("SEAL-4")
        self.assertEqual(len(self.stanza.messaggi), 1, self.stanza.messaggi)

    def test_il_verso_opposto_e_la_stessa_transizione_e_si_annuncia(self) -> None:
        self._turno("SEAL-2")
        self._turno("SEAL-4")
        self.stanza.messaggi.clear()
        self._turno("SEAL-2")
        self.assertEqual(len(self.stanza.messaggi), 1, self.stanza.messaggi)
        self.assertIn("clodia", self.stanza.messaggi[0])

    def test_un_tier_che_cambia_senza_spostare_il_coordinatore_tace(self) -> None:
        """SEAL-1 → SEAL-2: clodia regge entrambi, non è successo niente da dire."""
        self._turno("SEAL-1")
        self._turno("SEAL-2")
        self.assertEqual(self.stanza.messaggi, [])

    def test_i_partecipanti_si_chiedono_al_gateway_se_il_chiamante_non_li_ha(self) -> None:
        """`_start_turn` non ha la lista sotto mano: la fetch è sua, e solo alla
        transizione — non una chiamata di rete per turno."""
        letture: list[tuple] = []

        async def _open(tier, name):
            letture.append((tier, name))
            return {"meta": {"tier": "SEAL-4", "participants": PARTECIPANTI}}

        with patch.object(channels.topics_client, "async_open_topic", _open):
            _esegui(channels._annuncia_cambio_coordinatore(TIER, STANZA, "SEAL-2"))
            _esegui(channels._annuncia_cambio_coordinatore(TIER, STANZA, "SEAL-4"))
            _esegui(channels._annuncia_cambio_coordinatore(TIER, STANZA, "SEAL-4"))
        self.assertEqual(len(self.stanza.messaggi), 1, self.stanza.messaggi)
        self.assertEqual(letture, [(TIER, STANZA)])


class ChiamataDaiDueDispatcherTests(unittest.TestCase):
    """I turni di canale partono da due funzioni diverse: l'annuncio le attraversa
    entrambe, o la metà dei turni resta muta."""

    def setUp(self) -> None:
        channels._tier_visto_reset()
        self.viste: list[tuple] = []

        async def _spia(tier, name, tier_real, participants=None):
            self.viste.append((tier, name, tier_real, participants))

        async def _provider_ko(*a, **kw):
            return False

        for p in (patch.object(channels, "_annuncia_cambio_coordinatore", _spia),
                  patch.object(channels, "_provider_della_stanza_ancora_valido",
                               _provider_ko),
                  patch.object(channels.registry, "get_by_name", _get_by_name),
                  patch.object(channels, "_chat_busy", lambda chat_id: False)):
            p.start()
            self.addCleanup(p.stop)

    def test_start_turn_annuncia_prima_di_avviare_il_turno(self) -> None:
        avviato = _esegui(channels._start_turn(
            TIER, STANZA, "SEAL-4", _Spec("segretario"), "davide", "ciao", "direct"))
        self.assertFalse(avviato)
        self.assertEqual([v[:3] for v in self.viste], [(TIER, STANZA, "SEAL-4")])

    def test_run_topic_turn_annuncia_e_passa_i_partecipanti_che_ha_gia(self) -> None:
        meta = {"tier": "SEAL-4", "participants": PARTECIPANTI}
        responder, reply = _esegui(channels.run_topic_turn(
            TIER, STANZA, meta, trigger_text="ciao", responder_hint="segretario"))
        self.assertIsNone(responder)
        self.assertIsNone(reply)
        self.assertEqual(self.viste, [(TIER, STANZA, "SEAL-4", PARTECIPANTI)])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
