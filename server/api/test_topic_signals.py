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
    """`_pending_gates_for`: da dove viene il conteggio dei gate.

    La fonte è cambiata il 9 ago 2026. Era lo store dei workflow — il badge
    contava i gate di un run — e coi workflow rimossi sarebbe rimasto a zero per
    sempre: un badge dichiarato che nessuno alimenta. Ora la fonte è il gateway,
    che è dove i gate vivono.

    Cosa i test tengono fermo, oltre al conteggio: il badge **non deve poter
    rompere la pagina**. Se il gateway non risponde, la lista dei topic si vede
    lo stesso senza pallini, perché un aiuto che rompe ciò che aiuta non è un
    aiuto — ed è la direzione opposta a quella dei gate, dove un guasto deve
    fermare l'azione.
    """

    class _Resp:
        def __init__(self, code, body):
            self.status_code = code
            self._body = body

        def json(self):
            return self._body

    def _gates(self, richieste, code=200, principal="davide"):
        from . import gate as _gate
        with patch.object(_gate, "_gw",
                          lambda m, p, pr: self._Resp(code, {"requests": richieste})):
            return ts._pending_gates_for(principal)

    def test_a_pending_gate_lights_its_room(self):
        self.assertEqual(
            self._gates([{"verb": "topic.put", "chat": "chan:SEAL-1:acme:clodia"}]),
            {"SEAL-1/acme": 1})

    def test_two_gates_in_one_room_count_two(self):
        self.assertEqual(
            self._gates([{"chat": "chan:SEAL-1:acme:clodia"},
                         {"chat": "chan:SEAL-1:acme:segretario"}]),
            {"SEAL-1/acme": 2})

    def test_gates_of_different_rooms_do_not_mix(self):
        out = self._gates([{"chat": "chan:SEAL-1:acme:clodia"},
                           {"chat": "chan:SEAL-2:beta:clodia"}])
        self.assertEqual(out, {"SEAL-1/acme": 1, "SEAL-2/beta": 1})

    def test_a_gate_outside_a_room_lights_nothing(self):
        """Il turno di un job non ha una stanza da illuminare. Inventargliene
        una accenderebbe il pallino su un topic che non c'entra."""
        self.assertEqual(self._gates([{"chat": "job:42"}, {"chat": None}, {}]), {})

    def test_an_unreachable_gateway_does_not_break_the_list(self):
        from . import gate as _gate

        def rotto(m, p, pr):
            raise RuntimeError("connection refused")

        with patch.object(_gate, "_gw", rotto):
            self.assertEqual(ts._pending_gates_for("davide"), {})

    def test_an_error_response_is_not_a_count(self):
        self.assertEqual(self._gates([{"chat": "chan:SEAL-1:acme:clodia"}], code=500), {})


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


class AckSecondBoundaryTests(unittest.TestCase):
    """I `ts` sono al secondo: l'ack non deve inghiottire ciò che è arrivato
    nello stesso secondo ma dopo (falso negativo silenzioso, I4)."""

    ACK = "2026-07-31T10:00:05+00:00"

    def _sig(self, messages, read):
        with patch.object(ts.topics_client, "open_topic", return_value=_topic()), \
             patch.object(ts.topics_client, "list_messages", return_value=messages):
            return ts._topic_signal("davide", "SEAL-1", "ch", read, 0)

    def _read(self, acked=()):
        return {"visited": self.ACK, "mentions_upto": self.ACK,
                "mentions_acked": list(acked)}

    def test_acked_message_of_the_edge_second_is_read(self) -> None:
        msg = _msg(self.ACK, mentions=["davide"]) | {"id": "m-visto"}
        self.assertEqual(self._sig([msg], self._read(["m-visto"]))["actionable"], 0)

    def test_unacked_message_of_the_edge_second_stays_unread(self) -> None:
        # Stesso secondo dell'ack, ma non era ancora stato mostrato.
        msg = _msg(self.ACK, mentions=["davide"]) | {"id": "m-arrivato-dopo"}
        self.assertEqual(self._sig([msg], self._read(["m-visto"]))["actionable"], 1)

    def test_older_second_stays_read(self) -> None:
        msg = _msg("2026-07-31T10:00:04+00:00", mentions=["davide"]) | {"id": "m-vecchio"}
        self.assertEqual(self._sig([msg], self._read())["actionable"], 0)

    def test_newer_second_stays_unread(self) -> None:
        msg = _msg("2026-07-31T10:00:06+00:00", mentions=["davide"]) | {"id": "m-nuovo"}
        self.assertEqual(self._sig([msg], self._read())["actionable"], 1)

    def test_edge_ids_recorded_at_ack(self) -> None:
        msgs = [_msg("2026-07-31T10:00:04+00:00") | {"id": "a"},
                _msg(self.ACK) | {"id": "b"},
                _msg(self.ACK) | {"id": "c"},
                _msg("2026-07-31T10:00:06+00:00") | {"id": "d"}]
        with patch.object(ts.topics_client, "list_messages", return_value=msgs):
            ids = ts._edge_ids("SEAL-1", "ch", ts._ts(self.ACK))
        self.assertEqual(ids, ["b", "c"])


class WindowCoverageTests(unittest.TestCase):
    """Una mention non letta più vecchia della finestra non deve sparire dal
    badge solo perché non è stata guardata (I4)."""

    OLD_MENTION = "2026-07-30T09:00:00+00:00"

    def _sig(self, narrow, wide, read):
        calls = []

        def _list(tier, name, limit=200):
            calls.append(limit)
            return wide if limit > ts._MSG_WINDOW else narrow

        with patch.object(ts.topics_client, "open_topic", return_value=_topic()), \
             patch.object(ts.topics_client, "list_messages", side_effect=_list):
            return ts._topic_signal("davide", "SEAL-1", "ch", read, 0), calls

    def _full_narrow(self):
        # Finestra piena di traffico ordinario, tutto successivo all'ack.
        return [_msg(f"2026-07-31T10:{i // 60:02d}:{i % 60:02d}+00:00")
                for i in range(ts._MSG_WINDOW)]

    def test_widens_when_badge_would_be_zero_and_window_uncovered(self) -> None:
        narrow = self._full_narrow()
        wide = [_msg(self.OLD_MENTION, mentions=["davide"]) | {"id": "vecchia"}] + narrow
        read = {"visited": "2026-07-29T00:00:00+00:00",
                "mentions_upto": "2026-07-29T00:00:00+00:00"}
        sig, calls = self._sig(narrow, wide, read)
        self.assertEqual(sig["actionable"], 1)          # non persa
        self.assertEqual(calls, [ts._MSG_WINDOW, ts._MSG_WINDOW_MAX])

    def test_no_widening_when_window_already_covers_the_ack(self) -> None:
        narrow = self._full_narrow()
        # Ack DENTRO la finestra (che parte da 10:00:00): la finestra risale
        # oltre l'ack, quindi «0 mention» è verificato, non un limite di vista.
        read = {"visited": "2026-07-31T10:01:00+00:00",
                "mentions_upto": "2026-07-31T10:01:00+00:00"}
        sig, calls = self._sig(narrow, [], read)
        self.assertEqual(sig["actionable"], 0)
        self.assertEqual(calls, [ts._MSG_WINDOW])

    def test_no_widening_when_a_mention_is_already_found(self) -> None:
        narrow = self._full_narrow()[:-1] + [_msg("2026-07-31T10:03:20+00:00",
                                                  mentions=["davide"]) | {"id": "x"}]
        read = {"visited": "2026-07-29T00:00:00+00:00",
                "mentions_upto": "2026-07-29T00:00:00+00:00"}
        sig, calls = self._sig(narrow, [], read)
        self.assertEqual(sig["actionable"], 1)
        self.assertEqual(calls, [ts._MSG_WINDOW])

    def test_short_window_is_covered_by_definition(self) -> None:
        # Meno di _MSG_WINDOW messaggi = tutto il topic: niente riletture.
        read = {"visited": "2026-07-29T00:00:00+00:00",
                "mentions_upto": "2026-07-29T00:00:00+00:00"}
        sig, calls = self._sig([_msg("2026-07-31T10:00:00+00:00")], [], read)
        self.assertEqual(sig, {"actionable": 0, "activity": True})
        self.assertEqual(calls, [ts._MSG_WINDOW])


if __name__ == "__main__":
    unittest.main()
