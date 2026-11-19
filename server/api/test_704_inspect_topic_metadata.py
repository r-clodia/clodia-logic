"""#704 — `runtime.inspect_topic` is metadata only, and asks who is calling.

sysadmin inspects a room it does not take part in, from the widget. Until
#704 the route returned the latest 40 messages with their text, trusted `by`
from the body, and had no authentication: any process on the network could
name a high-clearance agent and read as it.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from . import channels as ch

SECRET = "il bilancio di Rossi chiude in perdita"


def _request(body: dict):
    async def _json():
        return body
    req = type("R", (), {})()
    req.json = _json
    req.headers = {}
    return req


MESSAGES = [
    {"author": "davide", "kind": "human", "text": SECRET, "ts": "2026-11-18T09:00:00Z"},
    {"author": "clodia", "kind": "agent", "text": SECRET, "ts": "2026-11-18T09:05:00Z"},
    {"author": "davide", "kind": "human", "text": SECRET, "ts": "2026-11-18T09:10:00Z"},
]
META = {"title": "Fatture", "status": "active", "type": "progetto", "owner": "davide",
        "participants": ["davide", "clodia"], "summary": SECRET,
        "tools": {"enabled": ["github.clone"],
                  "mcp": {"gdrive": {"resources": [{"uri": f"gdrive:folder/{SECRET}",
                                                    "effect": "allow"}]}}},
        "data": [{"kind": "datastore", "name": "contacts", "grants": {"clodia": "read"}}]}


class InspectTopicTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, body, *, signed="sysadmin", clearance_ok=True, gateway=False):
        from . import caller_auth
        spec = SimpleNamespace(name="sysadmin", type="bot")
        with patch.object(caller_auth, "caller_principal", return_value=signed), \
                patch.object(caller_auth, "is_paired_gateway", return_value=gateway), \
                patch.object(ch.registry, "get_by_name",
                             side_effect=lambda n: spec if n in ("sysadmin", "clodia") else None), \
                patch.object(ch, "_effective_clearance", return_value="SEAL-1"), \
                patch.object(ch, "_can_access", return_value=clearance_ok), \
                patch.object(ch.topics_client, "async_open_topic",
                             return_value={"meta": META}), \
                patch.object(ch.topics_client, "async_list_messages",
                             return_value=MESSAGES), \
                patch.object(ch.topics_client, "async_list_files",
                             return_value=[{"name": "a.pdf"}, {"name": "b.md"}]):
            return await ch.runtime_inspect_topic(_request(body))

    async def test_metadata_counts_and_timestamps_never_the_text(self):
        out = await self._call({"tier": "SEAL-1", "name": "fatture", "by": "sysadmin"})
        self.assertNotIn(SECRET, json.dumps(out, ensure_ascii=False))
        self.assertNotIn("messages", out)
        self.assertEqual(out["meta"]["participants"], ["davide", "clodia"])
        self.assertEqual(out["meta"]["agents"], ["clodia"])
        self.assertEqual(out["counts"], {"messages": 3, "messages_window": ch._INSPECT_WINDOW,
                                         "human": 2, "agent": 1, "system": 0, "files": 2})
        self.assertEqual(out["last_activity"]["last_message_at"], "2026-11-18T09:10:00Z")
        self.assertEqual(out["last_activity"]["last_agent_at"], "2026-11-18T09:05:00Z")
        self.assertEqual(out["last_activity"]["last_message_author"], "davide")
        self.assertEqual(out["toolbox"], {"enabled": ["github.clone"], "mcp": ["gdrive"]})
        self.assertEqual(out["imports"], [{"kind": "datastore", "name": "contacts",
                                           "grants": {"clodia": "read"}}])

    async def test_an_unsigned_caller_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            await self._call({"tier": "SEAL-1", "name": "fatture", "by": "sysadmin"},
                             signed=None)
        # `_require_declared_by` (#705): a `by` nobody signed is refused.
        self.assertEqual(cm.exception.status_code, 403)

    async def test_the_paired_gateway_may_declare_by(self):
        out = await self._call({"tier": "SEAL-1", "name": "fatture", "by": "sysadmin"},
                               signed=None, gateway=True)
        self.assertNotIn(SECRET, json.dumps(out, ensure_ascii=False))

    async def test_naming_another_agent_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            await self._call({"tier": "SEAL-1", "name": "fatture", "by": "clodia"},
                             signed="sysadmin")
        self.assertEqual(cm.exception.status_code, 403)

    async def test_above_the_clearance_the_topic_is_invisible(self):
        with self.assertRaises(HTTPException) as cm:
            await self._call({"tier": "SEAL-3", "name": "dossier", "by": "sysadmin"},
                             clearance_ok=False)
        self.assertEqual(cm.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
