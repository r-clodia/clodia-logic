"""Nessun canale nuovo si porta dietro un hook e un segreto.

clodia-platform#222 step 1 (clodia-tools#211). Il default `True` viveva in due
posti anche qui, e questo è il percorso che ha prodotto gli 8 hook misurati
sull'istanza: la webui crea il canale via `channel_create` → `create_topic`,
che dice al gateway `ensure_hook: false` (per non fare il giro
logic→gateway→logic) e poi provisiona l'hook **in casa** con `hooks_db.ensure`.
Rovesciare il default solo nel gateway avrebbe lasciato intatta esattamente la
strada da cui gli hook arrivavano.

Il terzo test è il motivo per cui il gate su `hook_enabled` esce dal percorso
`invoke_local`: l'invocazione locale è già partecipant-checked, la porta
pubblica è chiusa (#300), e con il default rovesciato quel gate avrebbe
risposto `409 hook disattivato` su ogni topic nuovo — una feature viva rotta
per chiudere una porta che non usa nessuno.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from ..api import channels, topics_client
from . import api, db


class _Resp:
    status_code = 200

    @staticmethod
    def json() -> dict:
        return {"meta": {"tier": "SEAL-1", "title": "acme"}}


class CreateTopicTests(unittest.TestCase):
    """Il client verso il gateway: cosa chiede, e cosa provisiona da sé."""

    def _create(self, **kw):
        http = MagicMock()
        http.post.return_value = _Resp()
        ensure = MagicMock(return_value=({}, None))
        with patch.object(topics_client, "_gw_http", http), \
             patch.object(topics_client, "_headers", lambda: {}), \
             patch.object(db, "ensure", ensure):
            topics_client.create_topic("SEAL-1", "acme", {"owner": "davide"}, **kw)
        return http.post.call_args.kwargs["json"], ensure

    def test_creating_a_channel_asks_for_no_hook(self):
        body, ensure = self._create()
        self.assertIs(body["hook_enabled"], False)
        ensure.assert_not_called()

    def test_an_explicit_request_is_still_served(self):
        body, ensure = self._create(hook_enabled=True)
        self.assertIs(body["hook_enabled"], True)
        ensure.assert_called_once()


class ChannelCreateTests(unittest.TestCase):
    """L'endpoint della webui: senza il flag nel body, non chiede l'hook."""

    def _post(self, body: dict) -> dict:
        request = SimpleNamespace(json=AsyncMock(return_value=body))
        created = AsyncMock(return_value={"title": "bando", "participants": []})
        with patch.object(channels, "_principal_from_request", return_value="owner"), \
             patch.object(channels.topics_client, "async_create_topic", created), \
             patch.object(channels.topics_client, "post_message", MagicMock()), \
             patch("server.api.topic_playbooks.welcome_message", return_value=""):
            asyncio.run(channels.channel_create(request))
        return created.call_args.kwargs

    def test_the_webui_path_asks_for_no_hook(self):
        self.assertIs(self._post({"name": "bando"})["hook_enabled"], False)

    def test_the_webui_path_forwards_an_explicit_yes(self):
        self.assertIs(
            self._post({"name": "bando", "hook_enabled": True})["hook_enabled"], True)


class InvokeLocalTests(unittest.IsolatedAsyncioTestCase):
    """Un topic senza hook resta invocabile localmente da un suo partecipante:
    l'hook si crea lì, su richiesta, che è il punto della issue."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (db._DIR, db._FILE, api.topics_client.open_topic,
                    api.topics_client.post_message, api._queue_turn,
                    api._principal_from_request)
        db._DIR = Path(self.tmp.name)
        db._FILE = db._DIR / "hooks.json"
        api.topics_client.open_topic = lambda _t, _n: {
            "meta": {"owner": "owner", "participants": ["owner", "clodia"],
                     "hook_enabled": False}}
        api.topics_client.post_message = lambda *a, **k: None

        async def _queue_turn(*_a, **_k):
            return True

        api._queue_turn = _queue_turn
        api._principal_from_request = lambda _req: "clodia"

    async def asyncTearDown(self) -> None:
        (db._DIR, db._FILE, api.topics_client.open_topic,
         api.topics_client.post_message, api._queue_turn,
         api._principal_from_request) = self.old
        self.tmp.cleanup()

    async def test_local_invocation_provisions_on_demand(self) -> None:
        request = SimpleNamespace(json=AsyncMock(return_value={"payload": "sync"}))
        result = await api.invoke_local("SEAL-1", "acme", request)
        self.assertTrue(result["triggered"])
        self.assertEqual(len(db.list_for_chat("SEAL-1", "acme")), 1)


if __name__ == "__main__":
    unittest.main()
