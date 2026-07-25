import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import routing_feedback


class RoutingFeedbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "corrections.jsonl"
        self.file_patch = patch.object(routing_feedback, "_FILE", self.path)
        self.file_patch.start()
        routing_feedback._CACHE = None

    def tearDown(self):
        self.file_patch.stop()
        routing_feedback._CACHE = None
        self.tmp.cleanup()

    def test_confirmation_is_positive_exemplar_and_selection_signal(self):
        routing_feedback.record_confirmation([0.1234567], "clodia", tier="seal-0", by="davide")

        row = json.loads(self.path.read_text("utf-8"))
        self.assertEqual(row["kind"], "confirm")
        self.assertEqual(row["agent"], "clodia")
        self.assertEqual(row["router_chose"], "clodia")
        self.assertEqual(routing_feedback.load_exemplars(),
                         [{"agent": "clodia", "vec": [0.123457]}])
        self.assertEqual(routing_feedback.stats()["selection_scores"]["clodia"]["score"], 1)

    def test_correction_penalizes_original_choice_and_trains_correct_agent(self):
        routing_feedback.record_correction(
            [0.5], "sysadmin", router_chose="clodia", tier="seal-0", by="davide")

        self.assertEqual(routing_feedback.load_exemplars(),
                         [{"agent": "sysadmin", "vec": [0.5]}])
        stats = routing_feedback.stats()
        self.assertEqual(stats["total_corrections"], 1)
        self.assertEqual(stats["selection_scores"]["clodia"],
                         {"confirmed": 0, "corrected": 1, "score": -1})


if __name__ == "__main__":
    unittest.main()
