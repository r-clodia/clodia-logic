from datetime import datetime, timedelta, timezone
import os
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
                # 0.85 (non 0.70): con e5 le cosine stanno in alto e il floor
                # di default è 0.80 — un esemplare a 0.70 sarebbe scartato come
                # rumore prima di arrivare al confronto sul decadimento.
                _ex("fiscalista", 0.85, ts=now.isoformat()),
            ],
            ["fiscalista", "esperto-bandi"],
            now=now,
        )

        # il vecchio (0.95) decade a 0.95 * 0.5^(360/90) = 0.059, il recente resta 0.85
        self.assertEqual(result["agent"], "fiscalista")

    def test_super_agent_is_eligible_for_corrected_routing(self):
        # modalità enforce: il default è shadow (traccia senza applicare)
        exemplars = [
            _ex("clodia", 0.92),
            _ex("clodia", 0.90),
            _ex("worker", 0.60),
        ]
        with (
            patch.dict(os.environ, {"RESPONDER_EXEMPLAR_MODE": "enforce"}),
            # Corpus sintetico da tre esemplari: sotto il supporto minimo del
            # gate di promozione (#186), che qui non è ciò che si misura.
            patch.object(responder_routing, "EXEMPLAR_MIN_SUPPORT", 0),
            patch.object(responder_routing, "EXEMPLAR_MIN_ACCURACY", 0.0),
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

    # --- Floor di similarità e shadow mode (fix del rilievo in review) -------

    def test_out_of_domain_vector_abstains_below_floor(self):
        """Un vincitore senza avversari ha margine 1.0: senza floor passerebbe
        anche a similarità da rumore. Misurato sul corpus reale: 6 frasi su 8
        fuori dominio venivano instradate con max_sim ~0.79."""
        exemplars = [_ex("clodia", 0.79), _ex("clodia", 0.78)]

        with patch.object(responder_routing, "EXEMPLAR_FLOOR", 0.80):
            self.assertIsNone(
                responder_routing._classify_exemplar_vector([1.0], exemplars, ["clodia"])
            )

        # sopra il floor la stessa configurazione decide
        with patch.object(responder_routing, "EXEMPLAR_FLOOR", 0.70):
            result = responder_routing._classify_exemplar_vector([1.0], exemplars, ["clodia"])
        self.assertIsNotNone(result)
        self.assertEqual(result["agent"], "clodia")

    def test_single_exemplar_at_noise_similarity_is_rejected(self):
        """Caso limite che motivava il floor: un solo esemplare, similarità 0.1,
        confidence 1.0 perché non ha avversari."""
        with patch.object(responder_routing, "EXEMPLAR_FLOOR", 0.80):
            self.assertIsNone(
                responder_routing._classify_exemplar_vector([0.1], [_ex("clodia", 1.0)], ["clodia"])
            )

    def test_shadow_mode_tracks_without_applying(self):
        """Default: la decisione è registrata ma NON restituita, così il routing
        per rilevanza resta al comando."""
        tracked = []
        with (
            patch.dict(os.environ, {"RESPONDER_EXEMPLAR_MODE": "shadow"}),
            patch.object(responder_routing, "embed_text", return_value=[1.0]),
            patch("server.api.routing_feedback.load_exemplars",
                       return_value=[_ex("clodia", 0.95), _ex("clodia", 0.94)]),
            patch("server.api.routing_feedback.record_decision",
                       side_effect=lambda *a, **k: tracked.append((a, k))),
        ):
            self.assertIsNone(responder_routing.pick_by_exemplar("m", ["clodia"]))

        self.assertEqual(len(tracked), 1)
        self.assertEqual(tracked[0][0][0], "exemplar-shadow")
        self.assertEqual(tracked[0][0][1], "clodia")

    def test_enforce_mode_applies_the_decision(self):
        with (
            patch.dict(os.environ, {"RESPONDER_EXEMPLAR_MODE": "enforce"}),
            # Vedi sopra: due esemplari non passano il gate di promozione, che
            # ha i suoi test in `TheEnforceSwitchIsGatedByTheMeasurementTests`.
            patch.object(responder_routing, "EXEMPLAR_MIN_SUPPORT", 0),
            patch.object(responder_routing, "EXEMPLAR_MIN_ACCURACY", 0.0),
            patch.object(responder_routing, "embed_text", return_value=[1.0]),
            patch("server.api.routing_feedback.load_exemplars",
                       return_value=[_ex("clodia", 0.95), _ex("clodia", 0.94)]),
            patch("server.api.routing_feedback.record_decision"),
        ):
            result = responder_routing.pick_by_exemplar("m", ["clodia"])

        self.assertIsNotNone(result)
        self.assertEqual(result[0], "clodia")

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


class TheEnforceSwitchIsGatedByTheMeasurementTests(unittest.TestCase):
    """clodia-platform#186 · la soglia di promozione era prosa, l'interruttore no.

    «≥ 70% su copertura non banale» stava in un commento, mentre
    `RESPONDER_EXEMPLAR_MODE=enforce` era una variabile d'ambiente libera: con
    l'accuratezza leave-one-out al 21% bastava scriverla e il routing
    peggiorava senza che niente collegasse la misura alla manopola che la
    presuppone. Il gate collega le due cose, e può solo IMPEDIRE.
    """

    def setUp(self) -> None:
        responder_routing._GATE_CACHE = None

    def tearDown(self) -> None:
        responder_routing._GATE_CACHE = None

    def _corpus(self, n: int = 40) -> list:
        return [_ex("clodia", 0.95) for _ in range(n)]

    def test_enforce_is_refused_when_the_measurement_is_below_threshold(self):
        """Il caso reale di oggi: 21–30% di accuratezza. `enforce` non basta."""
        with (
            patch.dict(os.environ, {"RESPONDER_EXEMPLAR_MODE": "enforce"}),
            patch.object(responder_routing, "evaluate_exemplars",
                         return_value={"predicted": 39, "accuracy": 0.28}),
        ):
            gate = responder_routing.exemplar_gate(self._corpus())
            self.assertFalse(responder_routing.exemplar_enforced(self._corpus()))

        self.assertEqual("enforce", gate["requested_mode"])
        self.assertEqual("shadow", gate["effective_mode"])
        self.assertIn("accuracy", gate["reason"])

    def test_below_threshold_the_router_is_not_hijacked(self):
        """La proprietà che conta non è il dizionario: è che il classificatore
        NON instrada. Senza il gate, qui tornava ('clodia', ...)."""
        tracked = []
        with (
            patch.dict(os.environ, {"RESPONDER_EXEMPLAR_MODE": "enforce"}),
            patch.object(responder_routing, "embed_text", return_value=[1.0]),
            patch.object(responder_routing, "evaluate_exemplars",
                         return_value={"predicted": 39, "accuracy": 0.28}),
            patch("server.api.routing_feedback.load_exemplars",
                  return_value=self._corpus()),
            patch("server.api.routing_feedback.record_decision",
                  side_effect=lambda *a, **k: tracked.append(a)),
        ):
            self.assertIsNone(responder_routing.pick_by_exemplar("m", ["clodia"]))

        self.assertEqual("exemplar-shadow", tracked[0][0])

    def test_enforce_holds_when_the_measurement_justifies_it(self):
        """Alzare un freno non è saldarlo: sopra soglia e con supporto, applica."""
        with (
            patch.dict(os.environ, {"RESPONDER_EXEMPLAR_MODE": "enforce"}),
            patch.object(responder_routing, "embed_text", return_value=[1.0]),
            patch.object(responder_routing, "evaluate_exemplars",
                         return_value={"predicted": 40, "accuracy": 0.86}),
            patch("server.api.routing_feedback.load_exemplars",
                  return_value=self._corpus()),
            patch("server.api.routing_feedback.record_decision"),
        ):
            result = responder_routing.pick_by_exemplar("m", ["clodia"])

        self.assertIsNotNone(result)
        self.assertEqual("clodia", result[0])

    def test_a_handful_of_votes_is_not_a_measurement(self):
        """100% su tre predizioni non è un'accuratezza: sotto il supporto
        minimo si resta shadow, e il motivo lo dice."""
        with patch.dict(os.environ, {"RESPONDER_EXEMPLAR_MODE": "enforce"}):
            gate = responder_routing.exemplar_gate(self._corpus(3))

        self.assertEqual("shadow", gate["effective_mode"])
        self.assertIn("support", gate["reason"])

    def test_in_shadow_the_measurement_is_not_even_computed(self):
        """Costo zero nella configurazione di default: non si paga una
        leave-one-out per confermare ciò che è già spento."""
        with (
            patch.dict(os.environ, {"RESPONDER_EXEMPLAR_MODE": "shadow"}),
            patch.object(responder_routing, "evaluate_exemplars") as evaluate,
        ):
            gate = responder_routing.exemplar_gate(self._corpus())

        evaluate.assert_not_called()
        self.assertEqual("shadow", gate["effective_mode"])

    def test_the_escape_hatch_is_explicit(self):
        """Chi vuole forzare (banco di prova, corpus sintetico) azzera le
        soglie: il gate resta un controllo, non un divieto."""
        with (
            patch.dict(os.environ, {"RESPONDER_EXEMPLAR_MODE": "enforce"}),
            # `RESPONDER_EXEMPLAR_MIN_ACCURACY=0` / `..._MIN_SUPPORT=0`
            # all'avvio: qui si patcha la costante perché è letta all'import,
            # come tutte le manopole di questo modulo.
            patch.object(responder_routing, "EXEMPLAR_MIN_ACCURACY", 0.0),
            patch.object(responder_routing, "EXEMPLAR_MIN_SUPPORT", 0),
        ):
            self.assertTrue(
                responder_routing.exemplar_enforced(self._corpus(3)))


class RoutingContextTest(unittest.TestCase):
    def test_window_includes_agents_and_humans_and_drops_older_turns(self):
        """router-notebook R7: cosa confronta il router semantico — la finestra di messaggi, contro i profili."""
        messages = [
            {"author": "owner", "kind": "human", "text": "troppo vecchio"},
            {"author": "owner", "kind": "human", "text": "serve un contratto"},
            {"author": "avvocato", "kind": "ai", "text": "quale controparte?"},
            {"author": "owner", "kind": "human", "text": "una startup"},
        ]

        text = responder_routing.compose_routing_context(
            messages,
            config=responder_routing.router_config.RouterConfig(
                recent_messages=3, threshold=0.80, margin=0.015
            ),
        )

        self.assertNotIn("troppo vecchio", text)
        self.assertEqual(
            text.splitlines(),
            [
                "[human @owner] serve un contratto",
                "[agent @avvocato] quale controparte?",
                "[human @owner] una startup",
            ],
        )

    def test_newest_message_survives_the_embedding_budget(self):
        messages = [
            {"author": "owner", "kind": "human", "text": "x" * 1990},
            {"author": "fiscalista", "kind": "ai", "text": "ULTIMO"},
        ]

        text = responder_routing.compose_routing_context(messages)

        self.assertLessEqual(len(text), responder_routing._MAX_QUERY_CHARS)
        self.assertIn("ULTIMO", text)


if __name__ == "__main__":
    unittest.main()
