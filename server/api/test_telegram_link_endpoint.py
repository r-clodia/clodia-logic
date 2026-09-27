"""`/api/topics/{tier}/{name}/tg-link` — la presa d'atto dell'owner sul cap SEAL
del channel Telegram deve ARRIVARE al gateway (clodia-platform#405).

Su un topic sopra SEAL-1 il gateway rifiuta il collegamento Telegram finché
l'owner non dichiara di prendere atto del downgrade (`accept_seal_downgrade`).
Questa rotta è l'unico percorso che quella dichiarazione può fare: se il campo
si ferma qui — e prima di #405 si fermava, perché il corpo veniva ricostruito
campo per campo con il solo `chat_id` — il browser mostra un consenso che
nessuno riceve, e l'owner ritenta all'infinito una cosa che ha già fatto.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from . import topics as T

META = {"tier": "SEAL-2", "owner": "davide",
        "participants": {"davide": "owner"}}


class _Req:
    def __init__(self, body=None):
        self._b = body

    async def json(self):
        if self._b is None:
            raise ValueError("no body")
        return self._b


class _Client:
    class TopicsClientError(RuntimeError):
        pass

    def __init__(self):
        self.chiamate: list = []

    def open_topic(self, tier, name):
        return {"meta": META}

    def telegram_link_status(self, tier, name):
        self.chiamate.append(("status", tier, name))
        return {"connected": False, "chat_id": None,
                "seal": {"tier": "SEAL-2", "cap": "SEAL-1",
                         "requires_ack": True, "ack": None}}

    def telegram_link_action(self, tier, name, action, **params):
        self.chiamate.append(("action", tier, name, action, params))
        return {"connected": action == "connect", "chat_id": params.get("chat_id")}

    def __getattr__(self, nome):
        if not nome.startswith("async_"):
            raise AttributeError(nome)
        sync = getattr(self, nome[len("async_"):])

        async def chiamata(*a, **k):
            return sync(*a, **k)

        return chiamata


class _Base(unittest.TestCase):
    def setUp(self):
        self.cli = _Client()

    def esegui(self, coro, chi="davide"):
        p = [patch.object(T, "topics_client", self.cli),
             patch.object(T, "_principal_from_request", lambda r: chi)]
        for x in p:
            x.start()
        try:
            return asyncio.run(coro)
        finally:
            for x in reversed(p):
                x.stop()


class SealAckForwardTests(_Base):
    def test_the_acknowledgement_reaches_the_gateway(self):
        self.esegui(T.telegram_link_action(
            "SEAL-2", "preventivi",
            _Req({"action": "connect", "chat_id": "-1001",
                  "accept_seal_downgrade": True})))
        self.assertEqual(
            self.cli.chiamate,
            [("action", "SEAL-2", "preventivi", "connect",
              {"chat_id": "-1001", "accept_seal_downgrade": True})])

    def test_without_it_nothing_is_invented(self):
        """Il default non è «accetto»: chi non dichiara niente non ha dichiarato
        niente, e il gateway deve vedere esattamente quello."""
        self.esegui(T.telegram_link_action(
            "SEAL-2", "preventivi", _Req({"action": "connect", "chat_id": "-1002"})))
        self.assertEqual(
            self.cli.chiamate,
            [("action", "SEAL-2", "preventivi", "connect",
              {"chat_id": "-1002", "accept_seal_downgrade": False})])

    def test_the_status_carries_the_seal_block_through(self):
        res = self.esegui(T.telegram_link_status("SEAL-2", "preventivi", _Req()))
        self.assertTrue(res["seal"]["requires_ack"])


if __name__ == "__main__":
    unittest.main()
