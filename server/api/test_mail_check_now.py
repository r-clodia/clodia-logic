"""An agent asks the courier to check the room's mail now (owner, 14 Nov 2026):
reading mail stays the courier's; the pass is limited to the room and
serialised with the periodic loop."""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import courier_api, mail_courier as MC

ROWS = [{"tier": "SEAL-1", "name": "acme", "account": "studio", "address": "s@x.it",
         "senders": 1, "senders_sig": "s"},
        {"tier": "SEAL-1", "name": "other", "account": "studio", "address": "s@x.it",
         "senders": 1, "senders_sig": "s"}]


class OnlyOneRoomTests(unittest.TestCase):
    def test_the_pass_is_limited_to_the_room(self):
        seen = []

        async def run_room(room, delivered=None, **kw):
            seen.append(room["name"])
            return 0
        with patch.object(MC, "rooms", lambda: list(ROWS)), \
                patch.object(MC, "run_room", run_room):
            asyncio.run(MC.run_cycle(only=("SEAL-1", "acme")))
        self.assertEqual(seen, ["acme"])

    def test_two_passes_never_overlap(self):
        stato = {"dentro": 0, "max": 0}

        async def run_room(room, delivered=None, **kw):
            stato["dentro"] += 1
            stato["max"] = max(stato["max"], stato["dentro"])
            await asyncio.sleep(0.02)
            stato["dentro"] -= 1
            return 0

        async def due():
            MC._CYCLE_LOCK = None
            await asyncio.gather(MC.run_cycle(), MC.run_cycle(only=("SEAL-1", "acme")))
        with patch.object(MC, "rooms", lambda: list(ROWS)), \
                patch.object(MC, "run_room", run_room):
            asyncio.run(due())
        self.assertEqual(stato["max"], 1)


class RouteTests(unittest.TestCase):
    def _app(self):
        app = FastAPI()
        app.include_router(courier_api.router)
        return TestClient(app)

    def test_the_secret_is_required(self):
        with patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": "s3"}):
            r = self._app().post("/internal/courier/check", json={"tier": "SEAL-1", "name": "acme"})
        self.assertEqual(r.status_code, 401)

    def test_it_runs_the_room(self):
        calls = []

        async def run_cycle(only=None):
            calls.append(only)
            return 2
        with patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": "s3"}), \
                patch.object(MC, "run_cycle", run_cycle):
            r = self._app().post("/internal/courier/check", headers={"X-Orchestrator-Secret": "s3"},
                                 json={"tier": "SEAL-1", "name": "acme"})
        self.assertEqual((r.status_code, r.json()), (200, {"delivered": 2}))
        self.assertEqual(calls, [("SEAL-1", "acme")])


if __name__ == "__main__":
    unittest.main()
