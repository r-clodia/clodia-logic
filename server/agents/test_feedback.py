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
                row = feedback.create(
                    agent="helper",
                    message_id="msg-1",
                    topic="SEAL-1/demo",
                    rating="thumbs_down",
                    by="owner",
                    comment="Troppo generica",
                )
                self.assertEqual(row["status"], "pending")
                self.assertTrue((Path(tmp) / "memory" / "feedback-lessons.json").is_file())

                learned = feedback.complete("helper", row["id"], "Usa esempi concreti.")
                self.assertEqual(learned["status"], "learned")
                self.assertEqual(
                    feedback.list_for("helper", topic="SEAL-1/demo")[0]["lesson"],
                    "Usa esempi concreti.",
                )
                self.assertIn("Usa esempi concreti.", feedback.prompt_section("helper"))

                self.assertTrue(feedback.delete("helper", row["id"]))
                self.assertEqual(feedback.list_for("helper"), [])

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
                    rating="thumbs_up", by="owner",
                )
                feedback.create(
                    agent="helper", message_id="b", topic="SEAL-0/two",
                    rating="thumbs_up", by="owner",
                )
                rows = feedback.list_for("helper", topic="SEAL-0/one")
                self.assertEqual([r["message_id"] for r in rows], ["a"])
