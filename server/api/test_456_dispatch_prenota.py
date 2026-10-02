"""I dispatcher di canale prenotano la sessione PRIMA di prepararla (#456).

Seconda finestra della stessa corsa (la prima è dentro `ChatManager.create()`,
coperta da `sdk_runtime/test_456_prenotazione_dispatch.py`): quando la sessione
è già viva, il dispatcher la recupera con `manager.get` e poi annuncia il
cambio coordinatore, ricontrolla il provider e costruisce il prompt — tutto
prima di `chat.send_user_message`, cioè prima che esista il
`_current_turn_task` che il reaper guarda.

**I dispatcher sono due**, ed è la lezione già pagata da #345, scritta nei
commenti di `run_topic_turn`: `_start_turn` serve la webui, `run_topic_turn`
serve Telegram, i trigger e i workflow. Coprirne uno lascia l'altro nella
finestra, e la finestra di `run_topic_turn` è pure più larga — c'è un
`list_messages` al gateway dentro la costruzione del prompt.

L'asserzione che conta non è «reserve è stato chiamato», è **quando**: dopo gli
await della preparazione sarebbe già tardi, ed è esattamente la forma del
difetto che #311 ha lasciato aperta.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from . import channels

TIER = "SEAL-1"
STANZA = "software-house"
PARTECIPANTI = ["davide", "clodia", "segretario"]
ATTESO = f"chan:{TIER}:{STANZA}:segretario"


def _esegui(coro):
    return asyncio.run(coro)


class _Spec:
    def __init__(self, name: str):
        self.name = name
        self.type = "bot"
        self.clearance = "SEAL-4"


class DueDispatcherPrenotanoTests(unittest.TestCase):
    """Entrambi i punti che avviano un turno di canale, come in #345."""

    def setUp(self) -> None:
        self.ordine: list[str] = []

        async def _annuncio(tier, name, tier_real, participants=None):
            self.ordine.append("annuncio")

        async def _provider_ko(*a, **kw):
            # Taglia corto: tutto ciò che viene dopo è preparazione, e la
            # prenotazione deve essere già stata presa.
            self.ordine.append("provider")
            return False

        def _reserve(chat_id, grace=None):
            self.ordine.append(f"reserve:{chat_id}")

        for p in (patch.object(channels, "_annuncia_cambio_coordinatore", _annuncio),
                  patch.object(channels, "_provider_della_stanza_ancora_valido",
                               _provider_ko),
                  patch.object(channels.registry, "get_by_name",
                               lambda nome: _Spec(nome)),
                  patch.object(channels, "_chat_busy", lambda chat_id: False),
                  patch.object(channels.manager, "reserve", _reserve)):
            p.start()
            self.addCleanup(p.stop)

    def test_start_turn_prenota_prima_di_preparare_il_turno(self) -> None:
        avviato = _esegui(channels._start_turn(
            TIER, STANZA, "SEAL-4", _Spec("segretario"), "davide", "ciao", "direct"))
        self.assertFalse(avviato)
        self.assertEqual(self.ordine[0], f"reserve:{ATTESO}",
                         f"la prenotazione non viene per prima: {self.ordine}")
        self.assertIn("provider", self.ordine)

    def test_run_topic_turn_prenota_prima_di_preparare_il_turno(self) -> None:
        """Il dispatcher di Telegram/trigger/workflow non passa da `_start_turn`:
        senza questa riga la finestra resta aperta proprio dove il prompt costa
        una lettura al gateway."""
        meta = {"tier": "SEAL-4", "participants": PARTECIPANTI}
        responder, reply = _esegui(channels.run_topic_turn(
            TIER, STANZA, meta, trigger_text="ciao", responder_hint="segretario"))
        self.assertIsNone(responder)
        self.assertIsNone(reply)
        self.assertEqual(self.ordine[0], f"reserve:{ATTESO}",
                         f"la prenotazione non viene per prima: {self.ordine}")
        self.assertIn("provider", self.ordine)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
