from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from . import topics


class TopicsIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "personal").mkdir()
        (self.root / "confidential").mkdir()
        self.old_root = topics.TOPICS_ROOT
        topics.TOPICS_ROOT = self.root

    def tearDown(self) -> None:
        topics.TOPICS_ROOT = self.old_root
        self.tmp.cleanup()

    def _topic(self, name: str, summary: str, title: str = "Project Alpha") -> Path:
        topic_dir = self.root / "personal" / name
        topic_dir.mkdir()
        (topic_dir / "meta.yaml").write_text(f"title: {title}\n", encoding="utf-8")
        (topic_dir / "summary.md").write_text(summary, encoding="utf-8")
        return topic_dir

    def test_rebuild_topic_index_extracts_card_fields(self) -> None:
        self._topic(
            "alpha",
            """# Project Alpha has a deliberately long opening sentence that should be cut down because cards need compact summaries for humans and agents.

## Prossimi passi
- Call Marta about the revised budget and collect approval.
- Draft the supplier checklist.
- Schedule the Friday review.
- This fourth item should not be included.
""",
        )

        payload = topics.rebuild_topic_index("personal", "alpha")

        self.assertEqual(payload["classification"], "personal")
        self.assertEqual(payload["name"], "alpha")
        self.assertEqual(payload["title"], "Project Alpha")
        self.assertLessEqual(len(payload["tldr"]), 400)
        self.assertEqual(
            payload["action_points"],
            [
                "Call Marta about the revised budget and collect approval.",
                "Draft the supplier checklist.",
                "Schedule the Friday review.",
            ],
        )

        index_path = self.root / ".index" / "personal__alpha.yaml"
        self.assertTrue(index_path.is_file())
        saved = yaml.safe_load(index_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["summary_url"], "/topics/personal/alpha/summary")

    def test_scan_uses_index_fields(self) -> None:
        self._topic(
            "beta",
            """Beta is ready for handoff.

- Confirm owner.
- Ship first draft.
""",
            title="Beta Topic",
        )

        rows = topics._scan("personal")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Beta Topic")
        self.assertEqual(rows[0]["tldr"], "Beta is ready for handoff.")
        self.assertEqual(rows[0]["action_points"], ["Confirm owner.", "Ship first draft."])
        self.assertEqual(rows[0]["summary_url"], "/topics/personal/beta/summary")

    def test_contact_agent_default_and_override(self) -> None:
        # Topic senza contact_agent → default "clodia"
        self._topic("gamma", "Gamma summary.\n\n- Gamma action.", title="Gamma")
        payload = topics.rebuild_topic_index("personal", "gamma")
        self.assertEqual(payload["contact_agent"], "clodia")
        self.assertEqual(topics._scan("personal")[0]["contact_agent"], "clodia")

        # Topic con contact_agent esplicito nel meta.yaml → override
        delta = self.root / "personal" / "delta"
        delta.mkdir()
        (delta / "meta.yaml").write_text(
            "title: Delta\ncontact_agent: dairio\n", encoding="utf-8")
        (delta / "summary.md").write_text("Delta.\n\n- Do x.", encoding="utf-8")
        payload = topics.rebuild_topic_index("personal", "delta")
        self.assertEqual(payload["contact_agent"], "dairio")

    def test_rebuild_all_topic_indexes(self) -> None:
        self._topic("alpha", "Alpha summary.\n\n- Alpha action.")
        self._topic("beta", "Beta summary.\n\n- Beta action.", title="Beta")

        result = topics.rebuild_all_topic_indexes()

        self.assertEqual(result["rebuilt"], 2)
        self.assertEqual(result["errors"], [])
        self.assertTrue((self.root / ".index" / "personal__alpha.yaml").is_file())
        self.assertTrue((self.root / ".index" / "personal__beta.yaml").is_file())
        self.assertEqual(
            sorted(topic["name"] for topic in result["topics"]),
            ["alpha", "beta"],
        )


    def test_recent_artifacts_empty_when_no_files_dir(self) -> None:
        topic = self._topic("no-files", "No files here.\n\n- Action.\n")
        artifacts = topics._extract_recent_artifacts(topic)
        self.assertEqual(artifacts, [])

    def test_recent_artifacts_returns_up_to_three(self) -> None:
        topic = self._topic("with-files", "Has files.\n\n- Action.\n")
        files_dir = topic / "files"
        files_dir.mkdir()
        for name in ("a.pdf", "b.docx", "c.txt", "d.md"):
            (files_dir / name).write_text(name, encoding="utf-8")

        artifacts = topics._extract_recent_artifacts(topic)

        self.assertLessEqual(len(artifacts), 3)
        for a in artifacts:
            self.assertIn("name", a)
            self.assertIn("path", a)
            self.assertIn("mtime_iso", a)
            self.assertTrue(a["path"].startswith("files/"))

    def test_recent_artifacts_excludes_dotfiles(self) -> None:
        topic = self._topic("dotfiles", "Dotfiles topic.\n\n- Action.\n")
        files_dir = topic / "files"
        files_dir.mkdir()
        (files_dir / ".hidden").write_text("hidden", encoding="utf-8")
        (files_dir / "visible.txt").write_text("visible", encoding="utf-8")

        artifacts = topics._extract_recent_artifacts(topic)

        names = [a["name"] for a in artifacts]
        self.assertNotIn(".hidden", names)
        self.assertIn("visible.txt", names)

    def test_recent_artifacts_in_rebuild_payload(self) -> None:
        topic = self._topic("payload-test", "Payload topic.\n\n- Action.\n")
        files_dir = topic / "files"
        files_dir.mkdir()
        (files_dir / "report.pdf").write_text("report", encoding="utf-8")

        payload = topics.rebuild_topic_index("personal", "payload-test")

        self.assertIn("recent_artifacts", payload)
        self.assertEqual(len(payload["recent_artifacts"]), 1)
        self.assertEqual(payload["recent_artifacts"][0]["name"], "report.pdf")

    def test_schema_version_in_payload(self) -> None:
        self._topic("versioned", "Versioned topic.\n\n- Action.\n")
        payload = topics.rebuild_topic_index("personal", "versioned")
        self.assertEqual(payload["schema_version"], topics.INDEX_SCHEMA_VERSION)

    def test_stale_schema_version_triggers_rebuild(self) -> None:
        topic = self._topic("stale", "Stale topic.\n\n- Action.\n")
        payload = topics.rebuild_topic_index("personal", "stale")
        # Scrivi un indice con schema_version obsoleta
        index_path = self.root / ".index" / "personal__stale.yaml"
        import yaml as _yaml
        stale = dict(payload)
        stale["schema_version"] = 0
        index_path.write_text(_yaml.safe_dump(stale), encoding="utf-8")
        # _index_is_current deve ritornare False
        summary_path = topic / "summary.md"
        meta_path = topic / "meta.yaml"
        self.assertFalse(
            topics._index_is_current(index_path, summary_path, meta_path, commit=None)
        )

    def test_scan_propagates_recent_artifacts(self) -> None:
        topic = self._topic("with-arts", "Arts topic.\n\n- Action.\n")
        files_dir = topic / "files"
        files_dir.mkdir()
        (files_dir / "deck.pptx").write_text("deck", encoding="utf-8")

        rows = topics._scan("personal")

        self.assertEqual(len(rows), 1)
        self.assertIn("recent_artifacts", rows[0])
        self.assertEqual(rows[0]["recent_artifacts"][0]["name"], "deck.pptx")


if __name__ == "__main__":
    unittest.main()
