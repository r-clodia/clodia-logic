"""The two routes that make the reasoning of a finished turn readable.

clodia-platform#484. The index says WHICH bubbles have stored reasoning (it is
what lights the 💭 in the UI; without it the button would open nothing); the
read returns the text of a single bubble.

The property that matters more than the format: reasoning quotes the channel's
content, so **it can be read only from inside the channel**. It is neither an
admin route nor a public one: it sits behind `_require_member`, the same guard
as the messages, and the guard fires BEFORE the store is touched.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from . import channels


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(channels.router)
    return TestClient(app)


_INDEX = "/clodia/channels/SEAL-2/preventivi-tomato/reasoning"
_ONE = _INDEX + "/m1"
_TOPIC = {"meta": {"tier": "SEAL-2", "participants": ["davide"]}}


class OnlyFromInsideTheChannel(unittest.TestCase):
    def test_the_index_is_for_participants(self) -> None:
        with patch.object(channels.topics_client, "async_open_topic",
                          new=AsyncMock(return_value=_TOPIC)), \
             patch.object(channels, "_require_member",
                          side_effect=HTTPException(403, "not a participant")), \
             patch.object(channels.reasoning_log, "index") as reader:
            r = _app().get(_INDEX)
        self.assertEqual(403, r.status_code)
        reader.assert_not_called()

    def test_the_read_is_for_participants(self) -> None:
        with patch.object(channels.topics_client, "async_open_topic",
                          new=AsyncMock(return_value=_TOPIC)), \
             patch.object(channels, "_require_member",
                          side_effect=HTTPException(403, "not a participant")), \
             patch.object(channels.reasoning_log, "read") as reader:
            r = _app().get(_ONE)
        self.assertEqual(403, r.status_code)
        reader.assert_not_called()

    def test_a_channel_that_does_not_exist_is_404(self) -> None:
        with patch.object(channels.topics_client, "async_open_topic",
                          new=AsyncMock(return_value=None)):
            self.assertEqual(404, _app().get(_INDEX).status_code)


class WhatItReturns(unittest.TestCase):
    def setUp(self) -> None:
        self._p = [
            patch.object(channels.topics_client, "async_open_topic",
                         new=AsyncMock(return_value=_TOPIC)),
            patch.object(channels, "_require_member", return_value="davide"),
        ]
        for p in self._p:
            p.start()
            self.addCleanup(p.stop)

    def test_the_index_lists_the_messages_that_have_one(self) -> None:
        with patch.object(channels.reasoning_log, "index",
                          return_value=["m1", "m7"]):
            r = _app().get(_INDEX)
        self.assertEqual(200, r.status_code)
        self.assertEqual(["m1", "m7"], r.json()["messages"])

    def test_the_read_carries_the_text_and_whether_it_was_cut(self) -> None:
        entry = {"message_id": "m1", "spawn": "clodia-7", "text": "thinking",
                 "truncated": False, "ts": "2026-10-01T10:00:00+00:00"}
        with patch.object(channels.reasoning_log, "read", return_value=entry):
            r = _app().get(_ONE)
        self.assertEqual(200, r.status_code)
        self.assertEqual("thinking", r.json()["text"])
        self.assertEqual("clodia-7", r.json()["spawn"])
        self.assertIs(False, r.json()["truncated"])

    def test_a_bubble_without_reasoning_is_404(self) -> None:
        """Never an empty 200: in the UI it would become a box opened on
        nothing, which is the ghost bubble we do not want."""
        with patch.object(channels.reasoning_log, "read", return_value=None):
            self.assertEqual(404, _app().get(_ONE).status_code)

    def test_the_store_is_queried_on_the_route_channel(self) -> None:
        with patch.object(channels.reasoning_log, "index",
                          return_value=[]) as reader:
            _app().get(_INDEX)
        self.assertEqual(("SEAL-2", "preventivi-tomato"), reader.call_args.args[:2])


class TheRoutesAreInTheApp(unittest.TestCase):
    """Actually mounted on the assembled app, not only in the module: a route
    declared in a router nobody includes does not exist (cf.
    `test_no_hook_surface`, same reason the OpenAPI is checked and not
    `app.routes`)."""

    def test_both_routes_are_served(self) -> None:
        from .. import main
        paths = main.create_app().openapi()["paths"]
        self.assertIn("/clodia/channels/{tier}/{name}/reasoning", paths)
        self.assertIn("/clodia/channels/{tier}/{name}/reasoning/{message_id}",
                      paths)


if __name__ == "__main__":
    unittest.main()
