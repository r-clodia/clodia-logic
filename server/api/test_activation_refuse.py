"""`activation: refuse` — il seed rifiuta il turno se ne ha già uno in corso.

R15 (issue clodia-platform#191): «coda, parallelo e rifiuto» sono meccaniche del
PROFILO del seed. Il rifiuto esisteva già come comportamento, ma era una
decisione di CHI POSTA (`skip_if_busy`, passato solo dal topic trigger dello
scheduler): un seed non poteva sceglierlo, e quindi il rifiuto non valeva per le
menzioni umane né per le deleghe fra agenti.

La guardia sta in `_start_turn`, che è il punto unico da cui passano tutti i rami
di dispatch — una sola volta, non una per chiamante.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from . import channels as ch


def _spec(activation: str, *, name: str = "dev"):
    return SimpleNamespace(name=name, multi_spawn=False, max_spawns=4,
                           activation=activation)


def _closing_spawn():
    """Sostituto di `_spawn_bg` che CHIUDE la coroutine invece di eseguirla:
    senza, il turno finto resta un `coroutine was never awaited` a video."""
    def _run(coro):
        coro.close()
    return patch.object(ch, "_spawn_bg", side_effect=_run)


def _chat(busy: bool):
    return SimpleNamespace(chat_id="chan:SEAL-1:ch:dev",
                           _lock=SimpleNamespace(locked=lambda: busy),
                           principal=None, origin=None)


class RefuseActivationTests(unittest.IsolatedAsyncioTestCase):

    async def _start(self, activation: str, busy: bool):
        posted: list[tuple] = []

        def _post(tier, name, author, text, **kw):
            posted.append((author, text, kw.get("kind")))
            return {"id": "1"}

        with patch.object(ch.manager, "get", return_value=_chat(busy)), \
             _closing_spawn() as spawn, \
             patch.object(ch.topics_client, "post_message", side_effect=_post), \
             patch.object(ch, "_channel_message", AsyncMock()), \
             patch.object(ch, "_reused_turn_prompt", return_value="prompt"), \
             patch.object(ch, "_tag_directive", return_value="dir"):
            ok = await ch._start_turn("SEAL-1", "ch", "SEAL-1", _spec(activation),
                                      "davide", "ciao", "direct")
        return ok, spawn, posted

    async def test_a_refusing_seed_does_not_start_a_second_turn(self) -> None:
        ok, spawn, _ = await self._start("refuse", busy=True)
        self.assertFalse(ok)
        spawn.assert_not_called()

    async def test_the_refusal_is_visible_in_the_room(self) -> None:
        """Non `_watch_report`: con `debug_watch` spento non lascia impronta, e
        un turno che non parte in silenzio è indistinguibile da un agente rotto —
        è il guasto che è costato mezza giornata su @fullstack-dev il 16 ago."""
        _, _, posted = await self._start("refuse", busy=True)
        self.assertEqual(len(posted), 1)
        author, text, kind = posted[0]
        self.assertEqual(kind, "system")
        self.assertIn("dev", text)
        self.assertIn("refuse", text)

    async def test_a_refusing_seed_that_is_free_answers(self) -> None:
        ok, spawn, posted = await self._start("refuse", busy=False)
        self.assertTrue(ok)
        spawn.assert_called_once()
        self.assertEqual(posted, [])

    async def test_a_queueing_seed_still_queues(self) -> None:
        """Il default non cambia comportamento: occupato o no, il turno parte e
        si accoda sul lock FIFO della sessione, come da sempre."""
        ok, spawn, posted = await self._start("queue", busy=True)
        self.assertTrue(ok)
        spawn.assert_called_once()
        self.assertEqual(posted, [])

    async def test_a_seed_without_the_field_queues(self) -> None:
        """Retrocompatibilità: uno spec senza `activation` (mock, seed vecchio)
        non deve inciampare nella guardia."""
        legacy = SimpleNamespace(name="dev", multi_spawn=False, max_spawns=4)
        with patch.object(ch.manager, "get", return_value=_chat(True)), \
             _closing_spawn() as spawn, \
             patch.object(ch, "_reused_turn_prompt", return_value="prompt"), \
             patch.object(ch, "_tag_directive", return_value="dir"):
            ok = await ch._start_turn("SEAL-1", "ch", "SEAL-1", legacy,
                                      "davide", "ciao", "direct")
        self.assertTrue(ok)
        spawn.assert_called_once()


class SessionBusyIsOnePlaceTests(unittest.TestCase):
    """`skip_if_busy` del chiamante e `activation: refuse` del seed rispondono
    alla stessa domanda — «questa sessione ha il lock tenuto?» — e la fanno con
    lo stesso helper. Due misure della stessa cosa avrebbero divergito."""

    def test_responder_busy_uses_the_shared_helper(self) -> None:
        import inspect
        self.assertIn("_chat_busy", inspect.getsource(ch._responder_busy))
        self.assertIn("_chat_busy", inspect.getsource(ch._start_turn))


if __name__ == "__main__":
    unittest.main()
