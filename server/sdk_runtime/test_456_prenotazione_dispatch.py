"""Il reaper non evince una chat il cui turno è ancora in preparazione (#456).

`_evict` chiede «c'è un turno in corso?» a `_current_turn_task`, che però nasce
dentro `send_user_message` (`session.py`), cioè **dopo** tutta la preparazione:
risoluzione del chat_id, `create()`, `start()` (skill_sync, workspace effimero,
sandbox), costruzione del prompt. In quella finestra la chat è nel registro con
`last_activity` vecchia — pre-popolata dalla history su disco — e nessun task:
il reaper la giudica innocentemente evincibile, chiama `stop()` (che azzera
`_opts_kwargs`), e il dispatcher trova la sessione morta appena arriva a
`send_user_message` → `RuntimeError("session not started")`.

Le finestre sono DUE e sono entrambe coperte qui:

1. dentro `ChatManager.create()`: `self._chats[cid] = chat` sta sotto il lock,
   ma `await chat.start()` è FUORI — è la finestra dell'incidente del 30/09
   (~950 ms fra «Workspace effimero creato» e l'evizione);
2. dentro `_start_turn` (`api/channels.py`): sessione già viva, recuperata con
   `manager.get`, e poi annunci/prompt prima del `send_user_message`.

Il rimedio è una PRENOTAZIONE a scadenza sul `chat_id`: non c'è nessun
`release()`, e questo è deliberato — un dispatcher che muore a metà non deve
lasciare una sessione immortale. La grazia scade da sé e si torna al
comportamento di oggi. `test_la_prenotazione_scade` è l'asserzione che tiene in
piedi questa scelta: senza di lei il meccanismo sarebbe un leak travestito da
fix.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from . import session

TTL = 1800.0


def _esegui(coro):
    return asyncio.run(coro)


class _ChatFinta:
    """Il minimo che `_evict` tocca di una sessione."""

    def __init__(self, cid: str = "chan:SEAL-1:stanza:tomato.content-creator",
                 idle: float = 3600.0, **_kw):
        self.id = cid
        self.kind = "tomato.content-creator"
        self.last_activity = datetime.now(timezone.utc) - timedelta(seconds=idle)
        self._current_turn_task = None
        self.unattended = False
        self.scope_tier = None
        self.title = ""
        self.fermata = False

    def read_history(self):
        return []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.fermata = True

    def to_dict(self) -> dict:
        return {"id": self.id}


class PrenotazioneTests(unittest.TestCase):
    """Il contratto della prenotazione, letto dal lato del reaper."""

    def setUp(self) -> None:
        self.manager = session.ChatManager()
        self.chat = _ChatFinta()
        self.manager._chats[self.chat.id] = self.chat
        p = patch.object(session.bus, "publish", AsyncMock())
        p.start()
        self.addCleanup(p.stop)

    def test_senza_prenotazione_la_chat_idle_viene_evinta(self) -> None:
        """La baseline: senza il meccanismo nuovo questo È l'incidente."""
        evinte = _esegui(self.manager.reap_idle(TTL))
        self.assertEqual(evinte, [self.chat.id])
        self.assertTrue(self.chat.fermata)

    def test_una_chat_prenotata_non_si_evince(self) -> None:
        """Idle da un'ora, nessun `_current_turn_task`: è esattamente la chat
        del 30/09. Prenotata, il reaper la lascia stare."""
        self.manager.reserve(self.chat.id)
        self.assertEqual(_esegui(self.manager.reap_idle(TTL)), [])
        self.assertFalse(self.chat.fermata)
        self.assertIn(self.chat.id, self.manager._chats)

    def test_la_prenotazione_scade(self) -> None:
        """Nessun `release()`: la grazia si scioglie da sola. Un dispatcher che
        muore fra la prenotazione e il turno non immortala la sessione."""
        self.manager.reserve(self.chat.id, grace=0.05)
        self.assertEqual(_esegui(self.manager.reap_idle(TTL)), [])
        _esegui(asyncio.sleep(0.08))
        self.assertEqual(_esegui(self.manager.reap_idle(TTL)), [self.chat.id])
        self.assertTrue(self.chat.fermata)

    def test_la_prenotazione_scaduta_non_resta_in_memoria(self) -> None:
        """Il registro delle prenotazioni non cresce per sempre: il tick che le
        ignora è anche quello che le butta."""
        self.manager.reserve("chat:mai-nata", grace=0.0)
        _esegui(self.manager.reap_idle(TTL))
        self.assertNotIn("chat:mai-nata", self.manager._reserved)

    def test_vale_anche_per_reap_dead(self) -> None:
        """La guardia sta in `_evict`, il punto unico: il criterio cambia, la
        regola «non toccare un turno che sta partendo» no. Una sessione in
        preparazione ha il subprocess ancora da aprire, cioè sembra morta."""
        self.manager.reserve(self.chat.id)
        with patch.object(session, "subprocess_morto", lambda chat: True):
            self.assertEqual(_esegui(self.manager.reap_dead()), [])
        self.assertFalse(self.chat.fermata)


class CreateProtegeLaPropriaStartTests(unittest.TestCase):
    """Finestra 1, quella dell'incidente: `create()` prenota da sé.

    Non basta che a prenotare sia il dispatcher di canale: `create()` ha altri
    chiamanti (job, API chat) e la finestra è dentro di lei — `start()` gira
    fuori dal lock, con la chat già nel registro.
    """

    def test_reap_idle_durante_start_non_evince_la_chat_appena_creata(self) -> None:
        manager = session.ChatManager()
        cid = "chan:SEAL-1:corso-confindustria:tomato.content-creator"
        reaped: list[list[str]] = []

        class _ChatCheVieneFalciataMentreParte(_ChatFinta):
            async def start(self) -> None:
                # Il reaper gira QUI: skill_sync + workspace + sandbox durano
                # ~950 ms, e in quel momento la chat è già in `_chats`.
                reaped.append(await manager.reap_idle(TTL))

        with (patch.object(session, "_refuse_if_abstract", lambda kind: None),
              patch.object(session, "_ensure_runtime_provider",
                           lambda kind, override: None),
              patch.object(session, "_runtime_class",
                           lambda kind, override: _ChatCheVieneFalciataMentreParte),
              patch.object(session.bus, "publish", AsyncMock()),
              patch("server.scoped_overrides.resolve",
                    lambda kind, chat_id=None, run_id=None: {})):
            chat = _esegui(manager.create(chat_id=cid, kind="tomato.content-creator"))

        self.assertEqual(reaped, [[]], "la chat è stata evinta mentre partiva")
        self.assertFalse(chat.fermata)
        self.assertIs(manager._chats.get(cid), chat)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
