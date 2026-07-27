from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from . import db


class HookStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir, self.old_file = db._DIR, db._FILE
        db._DIR = Path(self.tmp.name)
        db._FILE = db._DIR / "hooks.json"

    def tearDown(self) -> None:
        db._DIR, db._FILE = self.old_dir, self.old_file
        self.tmp.cleanup()

    def test_id_is_topic_slug_and_ensure_does_not_rotate(self) -> None:
        hook, secret = db.create("SEAL-1", "acme", "Acme", "owner")
        ensured, second_secret = db.ensure("SEAL-1", "acme", "Acme", "owner")
        self.assertEqual(hook["id"], "acme")
        self.assertEqual(ensured["id"], "acme")
        self.assertIsNotNone(secret)
        self.assertIsNone(second_secret)

    def test_slug_collision_across_tiers_is_rejected(self) -> None:
        db.create("SEAL-1", "acme", "Acme", "owner")
        with self.assertRaises(db.HookConflictError):
            db.create("SEAL-2", "acme", "Acme", "owner")

    def test_disabled_hook_is_not_recreated_by_ensure(self) -> None:
        db.create("SEAL-1", "acme", "Acme", "owner")
        self.assertTrue(db.revoke("acme"))
        hook, secret = db.ensure("SEAL-1", "acme", "Acme", "owner")
        self.assertFalse(hook["enabled"])
        self.assertIsNone(secret)

    def test_legacy_rows_migrate_without_trigger_agent(self) -> None:
        db._DIR.mkdir(parents=True, exist_ok=True)
        db._FILE.write_text(json.dumps([{
            "id": "opaque-id",
            "tier": "SEAL-1",
            "name": "acme",
            "trigger_agent": "clodia",
            "secret_hash": "hash",
        }]), "utf-8")
        row = db.get("acme")
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], "acme")
        self.assertNotIn("trigger_agent", row)


if __name__ == "__main__":
    unittest.main()
