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

    def test_generated_lesson_coexists_with_raw_comment(self) -> None:
        # #39: comment grezzo per audit, lesson generata iniettata. Solo la lesson
        # finisce in MEMORY.md; il commento resta nel record JSON.
        with TemporaryDirectory() as tmp:
            spec = SimpleNamespace(name="helper", agent_dir=tmp,
                                   memory=SimpleNamespace(dir="memory/"))
            with patch.object(feedback.registry, "get_by_name", return_value=spec):
                row = feedback.create(
                    agent="helper", message_id="a", topic="SEAL-0/one",
                    rating="thumbs_up", by="owner",
                    comment="Per Acme il fatturato è 3,2M",
                    lesson="In situazioni analoghe, continua a: citare la fonte.")
                self.assertEqual(row["comment"], "Per Acme il fatturato è 3,2M")
                self.assertEqual(row["lesson"],
                                 "In situazioni analoghe, continua a: citare la fonte.")
                self.assertEqual(row["status"], "learned")
                memory = (Path(tmp) / "memory" / "MEMORY.md").read_text()
                self.assertIn("continua a: citare la fonte.", memory)
                self.assertNotIn("Acme", memory)  # il commento grezzo NON è iniettato

    def test_empty_generated_lesson_is_audit_only(self) -> None:
        with TemporaryDirectory() as tmp:
            spec = SimpleNamespace(name="helper", agent_dir=tmp,
                                   memory=SimpleNamespace(dir="memory/"))
            with patch.object(feedback.registry, "get_by_name", return_value=spec):
                row = feedback.create(
                    agent="helper", message_id="a", topic="SEAL-0/one",
                    rating="thumbs_down", by="owner", comment="Vago", lesson="")
                self.assertEqual(row["status"], "recorded")
                self.assertEqual(row["lesson"], "")
                memory = (Path(tmp) / "memory" / "MEMORY.md").read_text()
                self.assertNotIn("Vago", memory)  # niente iniezione
