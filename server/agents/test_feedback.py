from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from . import feedback


class AgentFeedbackTest(TestCase):
    def test_feedback_lifecycle_is_persisted_in_agent_memory(self) -> None:
        with TemporaryDirectory() as tmp:
            spec = SimpleNamespace(
                name="helper",
                agent_dir=tmp,
                memory=SimpleNamespace(dir="memory/"),
            )
            with patch.object(feedback.registry, "get_by_name", return_value=spec):
                memory_path = Path(tmp) / "memory" / "MEMORY.md"
                memory_path.parent.mkdir(parents=True)
                memory_path.write_text("# Memory Index\n\nNota esistente.\n")
                row = feedback.create(
                    agent="helper",
                    message_id="msg-1",
                    topic="SEAL-1/demo",
                    rating="thumbs_down",
                    by="owner",
                    comment="Troppo generica",
                )
                self.assertEqual(row["status"], "learned")
                self.assertEqual(row["lesson"], "Troppo generica")
                self.assertTrue((Path(tmp) / "memory" / "feedback-lessons.json").is_file())
                self.assertEqual(
                    feedback.list_for("helper", topic="SEAL-1/demo")[0]["lesson"],
                    "Troppo generica",
                )
                self.assertIn("Troppo generica", feedback.prompt_section("helper"))
                memory = memory_path.read_text()
                self.assertIn("Nota esistente.", memory)
                self.assertIn("## Lesson learned dal feedback umano", memory)
                self.assertIn("Troppo generica", memory)
                feedback.prompt_section("helper")
                self.assertEqual(memory_path.read_text().count("## Lesson learned"), 1)

                self.assertTrue(feedback.delete("helper", row["id"]))
                self.assertEqual(feedback.list_for("helper"), [])
                self.assertNotIn("Troppo generica", memory_path.read_text())

    def test_feedback_for_other_topic_is_filtered(self) -> None:
        with TemporaryDirectory() as tmp:
            spec = SimpleNamespace(
                name="helper",
                agent_dir=tmp,
                memory=SimpleNamespace(dir="memory/"),
            )
            with patch.object(feedback.registry, "get_by_name", return_value=spec):
                feedback.create(
                    agent="helper", message_id="a", topic="SEAL-0/one",
                    rating="thumbs_up", by="owner", comment="Utile",
                )
                feedback.create(
                    agent="helper", message_id="b", topic="SEAL-0/two",
                    rating="thumbs_up", by="owner", comment="Chiaro",
                )
                rows = feedback.list_for("helper", topic="SEAL-0/one")
                self.assertEqual([r["message_id"] for r in rows], ["a"])

    def test_empty_comment_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            spec = SimpleNamespace(
                name="helper",
                agent_dir=tmp,
                memory=SimpleNamespace(dir="memory/"),
            )
            with patch.object(feedback.registry, "get_by_name", return_value=spec):
                with self.assertRaisesRegex(ValueError, "comment obbligatorio"):
                    feedback.create(
                        agent="helper", message_id="a", topic="SEAL-0/one",
                        rating="thumbs_up", by="owner", comment="  ",
                    )
