"""router-notebook R9: scavalcare un router CONVINTO ferma il turno sbagliato.

    «quando il router decide e non ha dubbi comunque l'umano deve poter aprire la
     routing box ed esprimere "come avrebbe scelto lui". In questo caso il turno
     dell'agent scelto dal router viene interrotto e parte il turno nuovo
     dell'agent segnalato dall'umano»
                                                        — Davide, 24 lug 2026

I tre pezzi esistevano già tutti — la correzione della routing box, l'interrupt
del canale, l'avvio di un turno per un agente nominato — e nessuno era collegato
agli altri: la correzione insegnava per la prossima volta e lasciava finire di
parlare l'agente sbagliato. Qui si verifica la SEQUENZA (correggi → interrompi →
re-instrada → registra) e il criterio di chi può azionarla: l'autore del
messaggio, con ripiego sull'owner del topic (clodia-platform#187).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from . import channels
from .test_channels import _a


class _Chat:
    def __init__(self, chat_id: str, running: bool = True) -> None:
        self.chat_id = chat_id
        self._running = running
        self.interrupted = False

    async def interrupt_current_turn(self) -> bool:
        self.interrupted = self._running
        return self._running


class OverruleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.agents = {
            "worker": _a("worker", "normal", "P1", "2026-02-01T00:00:00Z"),
            "worker-legacy": _a("worker-legacy", "normal", "P1",
                                "2026-02-01T00:00:01Z"),
            "accountant": _a("accountant", "normal", "P1", "2026-02-01T00:00:02Z"),
            "owner": _a("owner", "human", role="superadmin"),
            "guest": _a("guest", "human"),
        }
        self._orig_get = channels.registry.get_by_name
        channels.registry.get_by_name = lambda n: self.agents.get(n)
        self.meta = {"tier": "SEAL-1", "title": "Demo",
                     "participants": ["owner", "guest", "worker", "accountant"]}
        self.messages = [
            {"id": "m1", "kind": "human", "author": "owner",
             "text": "rifammi il bilancio"},
        ]

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig_get

    def _chats(self, *chats: _Chat):
        return patch.object(channels.manager, "list", return_value=list(chats))

    async def _correct(self, principal: str, chats: list[_Chat], *,
                       correct_agent: str = "accountant",
                       chosen: str | None = "worker") -> tuple[dict, AsyncMock]:
        request = SimpleNamespace(headers={}, json=AsyncMock(return_value={
            "tier": "SEAL-1", "name": "demo", "chosen": chosen,
            "correct_agent": correct_agent,
        }))
        with (
            patch.object(channels, "_principal_from_request", return_value=principal),
            patch.object(channels, "_require_contributor", return_value=principal),
            patch.object(channels.topics_client, "async_open_topic",
                         new_callable=AsyncMock, return_value={"meta": self.meta}),
            patch.object(channels.topics_client, "async_list_messages",
                         new_callable=AsyncMock, return_value=self.messages),
            patch.object(channels, "_latest_human_routing_context",
                         return_value="rifammi il bilancio"),
            patch.object(channels.responder_routing, "embed_text",
                         return_value=[0.1, 0.2]),
            patch.object(channels.routing_feedback, "record_correction"),
            patch.object(channels.topics_client, "async_post_message",
                         new_callable=AsyncMock, return_value={"id": "n1"}),
            patch.object(channels, "_channel_message", new_callable=AsyncMock),
            patch.object(channels, "_track_routing_decision"),
            patch.object(channels.bus, "publish", new_callable=AsyncMock),
            patch.object(channels, "_pick_responder",
                         side_effect=lambda _p, _t, tagged, _x, trace=None:
                         self.agents.get(tagged)),
            patch.object(channels, "_start_turn", new_callable=AsyncMock,
                         return_value=True) as start,
            self._chats(*chats),
        ):
            result = await channels.routing_correct(request)
        return result, start

    async def test_the_wrong_turn_stops_and_the_named_agent_starts(self) -> None:
        """La correzione non insegna solo: aggiusta il presente."""
        sbagliato = _Chat("chan:SEAL-1:demo:worker")
        result, start = await self._correct("owner", [sbagliato])

        self.assertTrue(sbagliato.interrupted)
        self.assertTrue(result["overruled"])
        self.assertEqual("accountant", result["responder"])
        self.assertEqual("accountant", result["learned"])
        self.assertEqual("accountant", start.await_args.args[3].name)

    async def test_only_the_author_or_the_topic_owner_may_overrule(self) -> None:
        """Un terzo contributor non distrugge il turno di una domanda non sua."""
        sbagliato = _Chat("chan:SEAL-1:demo:worker")
        result, start = await self._correct("guest", [sbagliato])

        self.assertFalse(sbagliato.interrupted)
        self.assertFalse(result["overruled"])
        self.assertEqual("not-authorized", result["reason"])
        start.assert_not_awaited()
        # …ma la correzione ha comunque insegnato: negarle il 200 renderebbe
        # indistinguibile «non ho imparato» da «non ho agito».
        self.assertEqual("accountant", result["learned"])

    async def test_the_topic_owner_is_the_fallback_when_the_author_is_absent(self) -> None:
        self.messages = [{"id": "m1", "kind": "human", "author": "guest",
                          "text": "rifammi il bilancio"}]
        self.meta = {**self.meta, "participants": {"owner": "owner",
                                                   "guest": "contributor",
                                                   "worker": "contributor",
                                                   "accountant": "contributor"}}
        sbagliato = _Chat("chan:SEAL-1:demo:worker")
        result, _ = await self._correct("owner", [sbagliato])

        self.assertTrue(sbagliato.interrupted)
        self.assertTrue(result["overruled"])

    async def test_a_finished_turn_is_taught_but_not_restarted(self) -> None:
        """Niente da interrompere → la correzione insegna e non riapre un turno
        che nessuno ha chiesto di nuovo."""
        finito = _Chat("chan:SEAL-1:demo:worker", running=False)
        result, start = await self._correct("owner", [finito])

        self.assertFalse(result["overruled"])
        self.assertEqual("turn-already-finished", result["reason"])
        start.assert_not_awaited()

    async def test_the_overrule_stops_the_chosen_agent_and_nobody_else(self) -> None:
        """Il prefisso di stringa fermerebbe l'omonimo più lungo; il seed no."""
        chosen = _Chat("chan:SEAL-1:demo:worker#2")
        omonimo = _Chat("chan:SEAL-1:demo:worker-legacy")
        altro_canale = _Chat("chan:SEAL-1:altro:worker")
        result, _ = await self._correct("owner", [chosen, omonimo, altro_canale])

        self.assertTrue(result["overruled"])
        self.assertTrue(chosen.interrupted)
        self.assertFalse(omonimo.interrupted)
        self.assertFalse(altro_canale.interrupted)

    async def test_the_channel_is_told_which_turn_was_stopped(self) -> None:
        """Un turno che si tronca in silenzio è un agente rotto, per chi guarda."""
        post = AsyncMock(return_value={"id": "n1"})
        with (
            patch.object(channels.topics_client, "async_post_message", post),
            patch.object(channels, "_channel_message", new_callable=AsyncMock),
        ):
            await channels._announce_overrule(
                "SEAL-1", "demo", self.meta, "owner", "worker", "accountant")

        testo = post.await_args.args[3]
        self.assertIn("@worker", testo)
        self.assertIn("@accountant", testo)
        self.assertIn("@owner", testo)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
