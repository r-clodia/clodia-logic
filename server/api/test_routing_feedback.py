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
        self.decisions_path = Path(self.tmp.name) / "decisions.jsonl"
        self.file_patch = patch.object(routing_feedback, "_FILE", self.path)
        self.decisions_patch = patch.object(
            routing_feedback, "_DECISIONS_FILE", self.decisions_path
        )
        self.file_patch.start()
        self.decisions_patch.start()
        routing_feedback._CACHE = None
        routing_feedback._CACHE_KEY = None

    def tearDown(self):
        self.decisions_patch.stop()
        self.file_patch.stop()
        routing_feedback._CACHE = None
        routing_feedback._CACHE_KEY = None
        self.tmp.cleanup()

    def test_confirmation_is_positive_exemplar_and_selection_signal(self):
        routing_feedback.record_confirmation([0.1234567], "clodia", tier="seal-0", by="davide")

        row = json.loads(self.path.read_text("utf-8"))
        self.assertEqual(row["kind"], "confirm")
        self.assertEqual(row["agent"], "clodia")
        self.assertEqual(row["router_chose"], "clodia")
        exemplar = routing_feedback.load_exemplars()[0]
        self.assertEqual(exemplar["agent"], "clodia")
        self.assertEqual(exemplar["vec"], [0.123457])
        self.assertEqual(exemplar["kind"], "confirm")
        self.assertTrue(exemplar["ts"])
        self.assertEqual(routing_feedback.stats()["selection_scores"]["clodia"]["score"], 1)

    def test_correction_penalizes_original_choice_and_trains_correct_agent(self):
        routing_feedback.record_correction(
            [0.5], "sysadmin", router_chose="clodia", tier="seal-0", by="davide")

        exemplar = routing_feedback.load_exemplars()[0]
        self.assertEqual(exemplar["agent"], "sysadmin")
        self.assertEqual(exemplar["vec"], [0.5])
        self.assertEqual(exemplar["kind"], "correction")
        stats = routing_feedback.stats()
        self.assertEqual(stats["total_corrections"], 1)
        self.assertEqual(stats["selection_scores"]["clodia"],
                         {"confirmed": 0, "corrected": 1, "score": -1})

    def test_orphan_agents_are_removed_from_corpus(self):
        rows = [
            {"agent": "clodia", "vec": [1.0], "kind": "confirm"},
            {"agent": "removed-agent", "vec": [0.5], "kind": "correction"},
        ]
        self.path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )

        exemplars = routing_feedback.load_exemplars({"clodia"})

        self.assertEqual([row["agent"] for row in exemplars], ["clodia"])
        persisted = [
            json.loads(line) for line in self.path.read_text("utf-8").splitlines()
        ]
        self.assertEqual([row["agent"] for row in persisted], ["clodia"])

    def test_decision_counters_are_grouped_by_origin_and_agent(self):
        routing_feedback.record_decision(
            "exemplar", "clodia", confidence=0.75, mode="exemplar"
        )
        routing_feedback.record_decision(
            "relevance", ["sysadmin", "clodia"], mode="relevance-multi"
        )

        stats = routing_feedback.stats()

        self.assertEqual(stats["decision_total"], 2)
        self.assertEqual(
            stats["decisions_by_origin"], {"exemplar": 1, "relevance": 1}
        )
        self.assertEqual(stats["decisions_by_agent"], {"clodia": 2, "sysadmin": 1})
        self.assertEqual(stats["exemplar_decision_share"], 0.5)

    def test_later_feedback_measures_accuracy_by_decision_origin(self):
        routing_feedback.record_decision(
            "exemplar", "clodia", mode="exemplar", topic="P0/ops"
        )
        routing_feedback.record_confirmation(
            [1.0], "clodia", topic="P0/ops"
        )
        routing_feedback.record_decision(
            "relevance", "sysadmin", mode="relevance", topic="P1/infra"
        )
        routing_feedback.record_correction(
            [0.8], "clodia", router_chose="sysadmin", topic="P1/infra"
        )

        stats = routing_feedback.stats()

        self.assertEqual(
            stats["feedback_by_origin"]["exemplar"],
            {
                "confirmations": 1,
                "corrections": 0,
                "total": 1,
                "accuracy": 1.0,
            },
        )
        self.assertEqual(
            stats["feedback_by_origin"]["relevance"],
            {
                "confirmations": 0,
                "corrections": 1,
                "total": 1,
                "accuracy": 0.0,
            },
        )


if __name__ == "__main__":
    unittest.main()
