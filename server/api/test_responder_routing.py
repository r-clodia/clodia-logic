from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from . import responder_routing


def _ex(agent, similarity, *, kind="correction", ts=None):
    return {
        "agent": agent,
        "vec": [similarity],
        "kind": kind,
        "ts": ts,
    }


class ExemplarRoutingTest(unittest.TestCase):
    def test_sdi_near_match_abstains_when_margin_is_too_small(self):
        exemplars = [
            _ex("esperto-bandi", 0.908),
            _ex("fiscalista", 0.900),
        ]
        with (
            patch(
                "server.api.routing_feedback.load_exemplars",
                return_value=exemplars,
            ),
            patch.object(responder_routing, "embed_text", return_value=[1.0]),
        ):
            result = responder_routing.pick_by_exemplar(
                "la fattura è stata trasmessa allo SDI",
                ["esperto-bandi", "fiscalista"],
                {"esperto-bandi", "fiscalista"},
            )

        self.assertIsNone(result)

    def test_repeated_corrections_are_cumulative(self):
        result = responder_routing._classify_exemplar_vector(
            [1.0],
            [
                _ex("fiscalista", 0.80),
                _ex("fiscalista", 0.79),
                _ex("fiscalista", 0.78),
                _ex("esperto-bandi", 0.90),
            ],
            ["fiscalista", "esperto-bandi"],
        )

        self.assertEqual(result["agent"], "fiscalista")
        self.assertEqual(result["support"], 3)

    def test_correction_has_more_weight_than_confirmation(self):
        result = responder_routing._classify_exemplar_vector(
            [1.0],
            [
                _ex("fiscalista", 0.80, kind="correction"),
                _ex("esperto-bandi", 0.90, kind="confirm"),
            ],
            ["fiscalista", "esperto-bandi"],
        )

        self.assertEqual(result["agent"], "fiscalista")

    def test_recent_feedback_outweighs_old_feedback(self):
        now = datetime(2026, 7, 30, tzinfo=timezone.utc)
        result = responder_routing._classify_exemplar_vector(
            [1.0],
            [
                _ex(
                    "esperto-bandi",
                    0.95,
                    ts=(now - timedelta(days=360)).isoformat(),
                ),
                _ex("fiscalista", 0.70, ts=now.isoformat()),
            ],
            ["fiscalista", "esperto-bandi"],
            now=now,
        )

        self.assertEqual(result["agent"], "fiscalista")

    def test_super_agent_is_eligible_for_corrected_routing(self):
        exemplars = [
            _ex("clodia", 0.92),
            _ex("clodia", 0.90),
            _ex("worker", 0.60),
        ]
        with (
            patch(
                "server.api.routing_feedback.load_exemplars",
                return_value=exemplars,
            ) as load,
            patch.object(responder_routing, "embed_text", return_value=[1.0]),
        ):
            result = responder_routing.pick_by_exemplar(
                "coordina il lavoro",
                ["clodia", "worker"],
                {"clodia", "worker"},
            )

        self.assertEqual(result[0], "clodia")
        load.assert_called_once_with({"clodia", "worker"})

    def test_leave_one_out_metrics_use_installed_vectors(self):
        exemplars = [
            {"agent": "a", "vec": [1.0, 0.0], "kind": "correction", "ts": None},
            {"agent": "a", "vec": [1.0, 0.0], "kind": "correction", "ts": None},
            {"agent": "b", "vec": [0.0, 1.0], "kind": "correction", "ts": None},
            {"agent": "b", "vec": [0.0, 1.0], "kind": "correction", "ts": None},
        ]

        metrics = responder_routing.evaluate_exemplars(exemplars, ["a", "b"])

        self.assertEqual(metrics["evaluated"], 4)
        self.assertEqual(metrics["predicted"], 4)
        self.assertEqual(metrics["correct"], 4)
        self.assertEqual(metrics["coverage"], 1.0)
        self.assertEqual(metrics["accuracy"], 1.0)


if __name__ == "__main__":
    unittest.main()
