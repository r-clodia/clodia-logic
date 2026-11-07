"""The topic's to-do list from the webui (clodia-platform#658).

  - `GET /api/topics/{tier}/{name}` is a plain read for the «Meta» popup:
    participants (or an admin) only, no agent turn, no side effect;
  - the to-do routes are for every participant, sign `by` with the principal
    verified here (never a body field), and turn a gateway 409 into a 409;
  - the deadline route refuses: the deadline is derived from the list.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from . import channels as C
from . import topics as T
from . import topics_client as real_client


META = {"tier": "SEAL-1", "owner": "davide",
        "participants": {"davide": "owner", "giovanni": "contributor"},
        "todos": [{"id": "t-1", "text": "Send the LOI", "due": "2026-11-20",
                   "assignee": None, "status": "open",
                   "updated_at": None, "updated_by": None}],
        "deadline": "2026-11-20"}


class _Req:
    def __init__(self, body=None):
        self._b = body or {}

    async def json(self):
        return self._b


class _Client:
    TopicsClientError = real_client.TopicsClientError
    TopicsConflictError = real_client.TopicsConflictError

    def __init__(self, conflict=False, error=None):
        self.calls = []
        self.conflict = conflict
        self.error = error

    def open_topic(self, tier, name):
        return {"tier": tier, "name": name, "meta": META, "todos_version": "v1",
                "links": [], "tldr": "Room", "updated_at": "2026-11-08T10:00:00+00:00"}

    def todo_action(self, tier, name, method, todo_id, payload):
        self.calls.append((method, todo_id, payload))
        if self.conflict:
            raise real_client.TopicsConflictError("todos changed")
        if self.error:
            raise self.error
        return {"todo": {"id": todo_id or "t-new"}, "todos": [], "deadline": None,
                "todos_version": "v2"}

    def __getattr__(self, nome):
        if not nome.startswith("async_"):
            raise AttributeError(nome)
        sync = getattr(self, nome[len("async_"):])

        async def chiamata(*a, **k):
            return sync(*a, **k)
        return chiamata


def _as(who, client, admin=False):
    return (patch.object(T, "topics_client", client),
            patch.object(T, "_principal_from_request", lambda r: who),
            patch.object(C, "_principal_from_request", lambda r: who),
            patch.object(T.admin, "is_admin", lambda n: admin))


def _run(ctx, coro_fn):
    with ctx[0], ctx[1], ctx[2], ctx[3]:
        return asyncio.run(coro_fn())


class MetaReadTests(unittest.TestCase):
    def test_a_participant_reads_meta_and_todos(self):
        res = _run(_as("giovanni", _Client()),
                   lambda: T.get_topic_meta("SEAL-1", "acme", _Req()))
        self.assertEqual(res["deadline"], "2026-11-20")
        self.assertEqual(res["todos_version"], "v1")
        self.assertEqual([t["id"] for t in res["todos"]], ["t-1"])
        self.assertFalse(res["is_admin"])

    def test_an_outsider_is_refused_an_admin_is_not(self):
        with self.assertRaises(HTTPException) as e:
            _run(_as("mallory", _Client()), lambda: T.get_topic_meta("SEAL-1", "acme", _Req()))
        self.assertEqual(e.exception.status_code, 403)
        res = _run(_as("root", _Client(), admin=True),
                   lambda: T.get_topic_meta("SEAL-1", "acme", _Req()))
        self.assertTrue(res["is_admin"])

    def test_the_read_calls_no_agent(self):
        """The popup must not cost a turn: the handler touches only the
        gateway's topic read — nothing in the module that starts a session."""
        import inspect
        src = inspect.getsource(T.get_topic_meta) + inspect.getsource(T._topic_for_reader)
        for forbidden in ("post_channel_message", "channel_trigger", "manager",
                          "access_log", "reconcile_membership", "send_message"):
            self.assertNotIn(forbidden, src)


class TodoWriteTests(unittest.TestCase):
    def test_a_participant_adds_and_by_is_the_verified_principal(self):
        cl = _Client()
        _run(_as("giovanni", cl), lambda: T.add_topic_todo(
            "SEAL-1", "acme", _Req({"text": "x", "base_version": "v1", "by": "davide"})))
        method, tid, payload = cl.calls[0]
        self.assertEqual((method, tid), ("POST", None))
        self.assertEqual(payload["by"], "giovanni")     # not the body's "davide"
        self.assertEqual(payload["base_version"], "v1")

    def test_update_sends_only_present_keys(self):
        cl = _Client()
        _run(_as("davide", cl), lambda: T.update_topic_todo(
            "SEAL-1", "acme", "t-1", _Req({"status": "done", "base_version": "v1"})))
        method, tid, payload = cl.calls[0]
        self.assertEqual((method, tid), ("POST", "t-1"))
        self.assertEqual(set(payload), {"status", "base_version", "by"})

    def test_remove(self):
        cl = _Client()
        _run(_as("davide", cl), lambda: T.remove_topic_todo(
            "SEAL-1", "acme", "t-1", _Req(), base_version="v1"))
        self.assertEqual(cl.calls[0][:2], ("DELETE", "t-1"))
        self.assertEqual(cl.calls[0][2]["base_version"], "v1")

    def test_an_outsider_may_not_write(self):
        cl = _Client()
        with self.assertRaises(HTTPException) as e:
            _run(_as("mallory", cl), lambda: T.add_topic_todo(
                "SEAL-1", "acme", _Req({"text": "x", "base_version": "v1"})))
        self.assertEqual(e.exception.status_code, 403)
        self.assertEqual(cl.calls, [])

    def test_conflict_is_409(self):
        with self.assertRaises(HTTPException) as e:
            _run(_as("davide", _Client(conflict=True)), lambda: T.add_topic_todo(
                "SEAL-1", "acme", _Req({"text": "x", "base_version": "old"})))
        self.assertEqual(e.exception.status_code, 409)

    def test_validation_error_keeps_its_class(self):
        err = real_client.TopicsClientError("gateway todos → HTTP 400", status=400,
                                            detail="todo: base_version is required")
        with self.assertRaises(HTTPException) as e:
            _run(_as("davide", _Client(error=err)), lambda: T.add_topic_todo(
                "SEAL-1", "acme", _Req({"text": "x"})))
        self.assertEqual(e.exception.status_code, 400)
        self.assertIn("base_version", e.exception.detail)


class DeadlineRouteTests(unittest.TestCase):
    def test_refused(self):
        with self.assertRaises(HTTPException) as e:
            asyncio.run(T.set_topic_deadline("SEAL-1", "acme", _Req({"deadline": "2026-11-20"})))
        self.assertEqual(e.exception.status_code, 400)
        self.assertIn("deadline is derived from todo items", e.exception.detail)


if __name__ == "__main__":
    unittest.main()
