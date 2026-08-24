from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from . import api, db


class _Request:
    def __init__(self, body: dict, headers: dict | None = None):
        self._body = body
        self.headers = headers or {}
        self.query_params = {}
        self.client = None

    async def json(self) -> dict:
        return self._body


class LocalHookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir, self.old_file = db._DIR, db._FILE
        db._DIR = Path(self.tmp.name)
        db._FILE = db._DIR / "hooks.json"
        self.old_open = api.topics_client.open_topic
        self.old_post = api.topics_client.post_message
        self.old_queue = api._queue_turn
        self.posts: list[dict] = []
        self.queued: list[dict] = []
        # Nessun `hook_enabled` nel meta: da clodia-tools#211 l'invocazione
        # locale non lo consulta più, e tenerlo qui farebbe credere il contrario.
        # Il caso col flag a False sta in test_no_automatic_hook.py.
        api.topics_client.open_topic = lambda _tier, _name: {
            "meta": {
                "owner": "owner",
                "participants": ["owner", "clodia"],
            }
        }
        api.topics_client.post_message = lambda *args, **kwargs: self.posts.append(kwargs)
        async def _queue_turn(*args, **kwargs):
            # `_queue_turn` è async da #106: accoda il turno DOPO aver letto il
            # topic dal gateway, e quella lettura non deve fermare l'event loop.
            self.queued.append(kwargs)
            return True

        api._queue_turn = _queue_turn
        # Identità autenticata dal session token (mai dal body): la controlliamo
        # patchando _principal_from_request.
        self.caller: str | None = None
        self.old_principal = api._principal_from_request
        api._principal_from_request = lambda _req: self.caller

    async def asyncTearDown(self) -> None:
        api.topics_client.open_topic = self.old_open
        api.topics_client.post_message = self.old_post
        api._queue_turn = self.old_queue
        api._principal_from_request = self.old_principal
        db._DIR, db._FILE = self.old_dir, self.old_file
        self.tmp.cleanup()

    async def test_participant_invocation_mentions_and_wakes_caller(self) -> None:
        self.caller = "clodia"
        result = await api.invoke_local(
            "SEAL-1", "acme", _Request({"payload": "sincronizza"}))
        self.assertTrue(result["triggered"])
        self.assertEqual(self.posts[0]["text"], "@clodia sincronizza")
        self.assertEqual(self.queued[0]["responder"], "clodia")

    async def test_non_participant_is_denied(self) -> None:
        self.caller = "ophelia"
        with self.assertRaises(HTTPException) as ctx:
            await api.invoke_local("SEAL-1", "acme", _Request({"payload": "x"}))
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_messaggero_can_invoke_without_membership(self) -> None:
        self.caller = "messaggero"
        result = await api.invoke_local(
            "SEAL-1", "acme", _Request({"payload": "mail"}))
        self.assertTrue(result["triggered"])
        self.assertEqual(self.queued[0]["responder"], "messaggero")

    async def test_unauthenticated_is_denied(self) -> None:
        # Nessuna identità nel session token → 401 (non 403): non si arriva
        # nemmeno al participant-check.
        self.caller = None
        with self.assertRaises(HTTPException) as ctx:
            await api.invoke_local("SEAL-1", "acme", _Request({"payload": "x"}))
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_body_caller_is_ignored(self) -> None:
        # PROVA DEL FIX: l'identità viene dal token (ophelia, non-participant),
        # non dal body (che dichiara clodia). Deve restare 403 → body.caller
        # è ignorato, niente impersonazione.
        self.caller = "ophelia"
        with self.assertRaises(HTTPException) as ctx:
            await api.invoke_local(
                "SEAL-1", "acme", _Request({"caller": "clodia", "payload": "x"}))
        self.assertEqual(ctx.exception.status_code, 403)

    # Il path REMOTO (`POST /hooks/{id}`, autorizzato dal solo segreto) non c'è
    # più: issue #300. Che sia chiuso lo verifica `test_public_ingress_is_closed`,
    # che guarda la superficie delle rotte invece della singola funzione.


if __name__ == "__main__":
    unittest.main()
