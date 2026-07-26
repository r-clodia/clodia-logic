from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import scoped_overrides


class ScopedOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "overrides.json"
        self.approvals = Path(self.tmp.name) / "approval-jtis.json"
        self.path_patch = patch.object(scoped_overrides, "_path", return_value=self.store)
        self.approval_patch = patch.object(
            scoped_overrides, "_approval_path", return_value=self.approvals)
        self.path_patch.start()
        self.approval_patch.start()

    def tearDown(self) -> None:
        self.path_patch.stop()
        self.approval_patch.stop()
        self.tmp.cleanup()

    def _create(self, **kwargs):
        body = {
            "agent": "clodia",
            "scope_kind": "topic",
            "scope_id": "SEAL-1/contracts",
            "ttl_minutes": 15,
            "requested_by": "ophelia",
            "approved_by": "davide",
            "approval_jti": "approval-1",
            "tools": ["email.send"],
        }
        body.update(kwargs)
        return scoped_overrides.create(**body)

    def test_topic_overlay_only_matches_same_channel_topic(self):
        self._create(capabilities=["legal-review"], rules=["concise"])
        hit = scoped_overrides.resolve(
            "clodia", chat_id="chan:SEAL-1:contracts:clodia")
        miss = scoped_overrides.resolve(
            "clodia", chat_id="chan:SEAL-1:other:clodia")
        self.assertEqual(hit["tools"], ["email.send"])
        self.assertEqual(hit["capabilities"], ["legal-review"])
        self.assertEqual(miss["tools"], [])

    def test_chat_and_run_scopes_do_not_leak(self):
        self._create(scope_kind="chat", scope_id="chat-7", tools=["fs.read"])
        self._create(scope_kind="run", scope_id="job:42", tools=["web.get"])
        self.assertEqual(
            scoped_overrides.resolve("clodia", chat_id="chat-7")["tools"],
            ["fs.read"],
        )
        self.assertEqual(
            scoped_overrides.resolve("clodia", run_id="job:42")["tools"],
            ["web.get"],
        )

    def test_expired_and_revoked_records_are_not_resolved(self):
        with patch.object(scoped_overrides.time, "time", return_value=1000):
            row = self._create(ttl_minutes=1)
        with patch.object(scoped_overrides.time, "time", return_value=1061):
            self.assertEqual(scoped_overrides.list_active("clodia"), [])
        row = self._create()
        self.assertIsNotNone(scoped_overrides.revoke(row["id"], agent="clodia"))
        self.assertEqual(scoped_overrides.list_active("clodia"), [])

    def test_empty_override_is_rejected(self):
        with self.assertRaises(ValueError):
            self._create(tools=[])

    def test_gate_approval_is_one_shot(self):
        with patch.object(scoped_overrides.time, "time", return_value=1000):
            self.assertTrue(scoped_overrides.consume_approval("jti-1", 1100))
            self.assertFalse(scoped_overrides.consume_approval("jti-1", 1100))
        with patch.object(scoped_overrides.time, "time", return_value=1101):
            self.assertTrue(scoped_overrides.consume_approval("jti-1", 1200))


if __name__ == "__main__":
    unittest.main()
