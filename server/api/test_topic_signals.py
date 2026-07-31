"""Test dei segnali per-principal (issue clodia-platform#83, DoD 1-12 lato server)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import topic_signals as ts


def _topic(owner="owner", participants=("owner", "davide")):
    return {"meta": {"owner": owner, "participants": list(participants)}}


def _msg(ts_iso, author="clodia", mentions=()):
    return {"ts": ts_iso, "author": author, "mentions": list(mentions), "kind": "human"}


class TopicSignalTests(unittest.TestCase):
    """_topic_signal: accensione, spegnimento, isolamento."""

    def _sig(self, principal, messages, read=None, gates=0, topic=None):
        with patch.object(ts.topics_client, "open_topic", return_value=topic or _topic()), \
             patch.object(ts.topics_client, "list_messages", return_value=messages):
            return ts._topic_signal(principal, "SEAL-1", "ch", read or {}, gates)

    # DoD 4: traffico ordinario, nessuna mention/gate → activity, actionable 0.
    def test_ordinary_traffic_is_dot_not_badge(self) -> None:
        msgs = [_msg(f"2026-07-31T10:00:{i:02d}+00:00") for i in range(20)]
        sig = self._sig("davide", msgs)
        self.assertEqual(sig, {"actionable": 0, "activity": True})

    # DoD 5 (lato dati): mention + traffico → actionable conta SOLO gli item.
    def test_actionable_counts_items_not_messages(self) -> None:
        msgs = [_msg("2026-07-31T10:00:00+00:00"),
                _msg("2026-07-31T10:00:01+00:00", mentions=["davide"]),
                _msg("2026-07-31T10:00:02+00:00"),
                _msg("2026-07-31T10:00:03+00:00", mentions=["davide", "anna"])]
        sig = self._sig("davide", msgs)
        self.assertEqual(sig["actionable"], 2)

    # DoD 6: gate assegnato a me senza alcuna mention → badge presente.
    def test_gate_without_mentions_still_badges(self) -> None:
        sig = self._sig("davide", [], gates=1)
        self.assertEqual(sig["actionable"], 1)

    # DoD 10: la visita spegne activity ma NON il gate.
    def test_visit_clears_dot_not_gate(self) -> None:
        msgs = [_msg("2026-07-31T10:00:00+00:00")]
        read = {"visited": "2026-07-31T11:00:00+00:00"}
        sig = self._sig("davide", msgs, read=read, gates=1)
        self.assertEqual(sig, {"actionable": 1, "activity": False})

    # La visita NON spegne le mention (solo l'ack mentions_upto lo fa).
    def test_visit_does_not_clear_mentions(self) -> None:
        msgs = [_msg("2026-07-31T10:00:00+00:00", mentions=["davide"])]
        read = {"visited": "2026-07-31T11:00:00+00:00"}
        sig = self._sig("davide", msgs, read=read)
        self.assertEqual(sig["actionable"], 1)
        self.assertFalse(sig["activity"])

    def test_mentions_ack_clears_mentions(self) -> None:
        msgs = [_msg("2026-07-31T10:00:00+00:00", mentions=["davide"])]
        read = {"visited": "2026-07-31T11:00:00+00:00",
                "mentions_upto": "2026-07-31T11:00:00+00:00"}
        sig = self._sig("davide", msgs, read=read)
        self.assertEqual(sig, {"actionable": 0, "activity": False})

    # I miei stessi messaggi non accendono nulla.
    def test_own_messages_do_not_signal(self) -> None:
        msgs = [_msg("2026-07-31T10:00:00+00:00", author="davide", mentions=["davide"])]
        sig = self._sig("davide", msgs)
        self.assertEqual(sig, {"actionable": 0, "activity": False})

    # DoD 2 / I2: non participant → None (omesso, nemmeno zero).
    def test_non_participant_is_omitted(self) -> None:
        sig = self._sig("estraneo", [_msg("2026-07-31T10:00:00+00:00")])
        self.assertIsNone(sig)

    # I4: membership ok ma messaggi illeggibili → si segnala comunque.
    def test_unreadable_messages_fail_safe(self) -> None:
        with patch.object(ts.topics_client, "open_topic", return_value=_topic()), \
             patch.object(ts.topics_client, "list_messages", side_effect=RuntimeError("boom")):
            sig = ts._topic_signal("davide", "SEAL-1", "ch", {}, 1)
        self.assertEqual(sig, {"actionable": 1, "activity": True})

    # I4: timestamp malformato → nel dubbio il messaggio conta.
    def test_malformed_ts_counts(self) -> None:
        msgs = [_msg("non-una-data", mentions=["davide"])]
        read = {"visited": "2026-07-31T11:00:00+00:00",
                "mentions_upto": "2026-07-31T11:00:00+00:00"}
        sig = self._sig("davide", msgs, read=read)
        self.assertEqual(sig["actionable"], 1)
        self.assertTrue(sig["activity"])


class PendingGatesTests(unittest.TestCase):
    """_pending_gates_for: assegnazione e riassegnazione (DoD 11-12)."""

    def _runs(self, runs):
        with patch.object(ts.wf_store, "list_runs", return_value=runs):
            return ts._pending_gates_for("davide")

    def test_gate_assigned_to_me(self) -> None:
        runs = [{"gate_pending": True, "wf_owner": "davide",
                 "topic": {"tier": "SEAL-1", "name": "wf-1"}}]
        self.assertEqual(self._runs(runs), {"SEAL-1/wf-1": 1})

    # DoD 11: gate risolto (gate_pending falso) → sparisce.
    def test_resolved_gate_disappears(self) -> None:
        runs = [{"gate_pending": False, "wf_owner": "davide",
                 "topic": {"tier": "SEAL-1", "name": "wf-1"}}]
        self.assertEqual(self._runs(runs), {})

    # DoD 12: riassegnato ad altri → spento per me (e acceso per lui).
    def test_reassigned_gate_moves(self) -> None:
        runs = [{"gate_pending": True, "wf_owner": "anna",
                 "topic": {"tier": "SEAL-1", "name": "wf-1"}}]
        self.assertEqual(self._runs(runs), {})
        with patch.object(ts.wf_store, "list_runs", return_value=runs):
            self.assertEqual(ts._pending_gates_for("anna"), {"SEAL-1/wf-1": 1})

    def test_fallback_requested_by(self) -> None:
        runs = [{"gate_pending": True, "wf_owner": "",
                 "requested_by": "davide",
                 "topic": {"tier": "SEAL-1", "name": "wf-2"}}]
        self.assertEqual(self._runs(runs), {"SEAL-1/wf-2": 1})


class ReadStateTests(unittest.TestCase):
    """Stato di lettura per-principal (DoD 1 / I1)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch = patch.object(ts, "data_path",
                                  side_effect=lambda rel: Path(self.tmp.name) / rel)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_state_is_per_principal(self) -> None:
        ts._save_state("alice", {"SEAL-1/ch": {"visited": "2026-07-31T10:00:00+00:00"}})
        self.assertEqual(ts._load_state("bob"), {})
        self.assertIn("SEAL-1/ch", ts._load_state("alice"))

    def test_missing_state_is_empty(self) -> None:
        self.assertEqual(ts._load_state("nessuno"), {})


if __name__ == "__main__":
    unittest.main()
