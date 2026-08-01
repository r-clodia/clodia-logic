"""Issue #86 — un turno morto per errore infra deve LASCIARE TRACCIA nel canale.

Prima il fallimento veniva solo loggato: l'utente vedeva il proprio messaggio
senza risposta e lo interpretava come "l'agente mi ha ignorato" o come un
problema di routing (#21). La nota è `kind="system"`, così non viene contata
come risposta AI e non innesca altri turni.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from . import channels


class TurnFailureNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_notice_is_posted_as_system_message(self):
        posts: list[tuple] = []
        with patch.object(channels.topics_client, "post_message",
                          side_effect=lambda *a, **kw: posts.append((a, kw))), \
             patch.object(channels, "_channel_message", new=AsyncMock()) as notify:
            await channels._post_turn_failure_notice(
                "P0", "ops", "avvocato",
                RuntimeError("Working directory does not exist: /datadir/spawns/avvocato-1"))
        self.assertEqual(len(posts), 1)
        args, kwargs = posts[0]
        self.assertEqual(args[:3], ("P0", "ops", "runtime"))
        self.assertEqual(kwargs.get("kind"), "system")
        text = args[3]
        self.assertIn("avvocato", text)
        self.assertIn("infrastruttura", text)
        self.assertIn("RuntimeError", text)
        self.assertIn("Working directory does not exist", text)
        notify.assert_awaited_once()

    async def test_notice_failure_never_propagates(self):
        """Se il gateway topics è giù la nota si perde, ma non deve sollevare."""
        with patch.object(channels.topics_client, "post_message",
                          side_effect=RuntimeError("gateway down")), \
             patch.object(channels, "_channel_message", new=AsyncMock()):
            await channels._post_turn_failure_notice("P0", "ops", "avvocato",
                                                     RuntimeError("boom"))

    async def test_failed_turn_triggers_the_notice(self):
        """Il seam è agganciato al punto giusto di `_run_and_post_response`."""
        class BrokenChat:
            principal = ""

            async def send_user_message(self, _prompt: str) -> str:
                raise RuntimeError("boom")

        with patch.object(channels.topics_client, "list_messages", return_value=[]), \
             patch.object(channels, "_typing", new=AsyncMock()), \
             patch.object(channels, "_post_turn_failure_notice", new=AsyncMock()) as notice:
            reply = await channels._run_and_post_response(
                "P0", "ops", "avvocato", BrokenChat(), "prompt")
        self.assertIsNone(reply)
        notice.assert_awaited_once()
        self.assertEqual(notice.await_args.args[:3], ("P0", "ops", "avvocato"))


if __name__ == "__main__":
    unittest.main()