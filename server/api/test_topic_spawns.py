"""Test dell'albero spawn di un topic (issue clodia-platform#99).

Copre la DoD di sicurezza: scoping al topic corrente, accesso ai soli membri,
payload minimo (nome + stato, mai contenuto di lavoro) e mappa del semaforo
sugli stati che il backend già produce.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from . import channels
from . import spawns as ts


def _chat(chat_id: str, status: str = "idle",
          last_activity: str = "2026-08-02T10:00:00+00:00", **extra):
    """Sessione finta: `to_dict()` porta anche campi sensibili, per verificare
    che l'endpoint NON li rimbalzi al client."""
    data = {
        "chat_id": chat_id,
        "status": status,
        "last_activity": last_activity,
        "principal": "owner",
        "total_tokens": {"input": 1000, "output": 500, "runs": 3},
        **extra,
    }
    return SimpleNamespace(chat_id=chat_id, to_dict=lambda: data)


class _Req:
    headers: dict = {}


TOPIC = {"meta": {"tier": "SEAL-1", "owner": "owner",
                  "participants": ["owner", "fullstack-dev"]}}


class TopicSpawnsTests(unittest.TestCase):
    def _call(self, sessions, principal="owner", topic=TOPIC, tier="SEAL-1",
              name="software-house"):
        with (
            patch.object(ts.manager, "list", return_value=sessions),
            patch.object(ts.topics_client, "open_topic", return_value=topic),
            patch.object(channels, "_principal_from_request", return_value=principal),
        ):
            return ts.channel_spawns(tier, name, _Req())

    # --- scoping -----------------------------------------------------------
    def test_only_current_topic_sessions(self) -> None:
        res = self._call([
            _chat("chan:SEAL-1:software-house:fullstack-dev#1"),
            _chat("chan:SEAL-1:software-house:fullstack-dev#2"),
            _chat("chan:SEAL-3:altro-topic:fullstack-dev#1"),
            _chat("chan:SEAL-1:altro-nome:clodia"),
            _chat("spawn:sysadmin-4"),
        ])
        self.assertEqual([r["label"] for r in res["spawns"]],
                         ["fullstack-dev#1", "fullstack-dev#2"])

    def test_non_member_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as cm:
            self._call([], principal="estraneo")
        self.assertEqual(cm.exception.status_code, 403)

    def test_anonymous_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as cm:
            self._call([], principal=None)
        self.assertEqual(cm.exception.status_code, 401)

    def test_missing_topic_is_404(self) -> None:
        with self.assertRaises(HTTPException) as cm:
            self._call([], topic=None)
        self.assertEqual(cm.exception.status_code, 404)

    # --- payload minimo ----------------------------------------------------
    def test_payload_carries_only_name_and_state(self) -> None:
        res = self._call([_chat("chan:SEAL-1:software-house:fullstack-dev#2")])
        row = res["spawns"][0]
        self.assertEqual(set(row), {"agent", "instance", "label", "state"})
        self.assertEqual(row["agent"], "fullstack-dev")
        self.assertEqual(row["instance"], 2)
        # nessuna traccia di token/principal/last_activity nel payload
        for leaky in ("principal", "tokens_in", "tokens_out", "runs",
                      "last_activity", "chat_id", "topic"):
            self.assertNotIn(leaky, row)

    def test_seed_without_ordinal_has_no_instance(self) -> None:
        res = self._call([_chat("chan:SEAL-1:software-house:clodia")])
        self.assertEqual(res["spawns"][0]["agent"], "clodia")
        self.assertIsNone(res["spawns"][0]["instance"])

    # --- semaforo ----------------------------------------------------------
    def test_state_map_covers_the_four_colours(self) -> None:
        res = self._call([
            _chat("chan:SEAL-1:software-house:a#1", status="thinking",
                  last_activity="2999-01-01T00:00:00+00:00"),   # 🟢 running
            _chat("chan:SEAL-1:software-house:b#1", status="thinking",
                  last_activity="2000-01-01T00:00:00+00:00"),   # 🟠 fermo > 180s
            _chat("chan:SEAL-1:software-house:c#1", status="cancelling"),  # 🟠
            _chat("chan:SEAL-1:software-house:d#1", status="error"),       # 🔴
            _chat("chan:SEAL-1:software-house:e#1", status="idle"),        # ⚪
        ])
        self.assertEqual({r["agent"]: r["state"] for r in res["spawns"]}, {
            "a": "running", "b": "blocked", "c": "blocked",
            "d": "error", "e": "idle",
        })

    def test_stopped_leaves_the_tree(self) -> None:
        res = self._call([
            _chat("chan:SEAL-1:software-house:a#1", status="stopped"),
            _chat("chan:SEAL-1:software-house:a#2", status="idle"),
        ])
        self.assertEqual([r["label"] for r in res["spawns"]], ["a#2"])

    def test_unexpected_status_is_neutral_not_green(self) -> None:
        res = self._call([_chat("chan:SEAL-1:software-house:a#1", status="boh")])
        self.assertEqual(res["spawns"][0]["state"], "unknown")

    # --- robustezza --------------------------------------------------------
    def test_sorted_by_agent_then_instance(self) -> None:
        res = self._call([
            _chat("chan:SEAL-1:software-house:zeta#2"),
            _chat("chan:SEAL-1:software-house:alfa#10"),
            _chat("chan:SEAL-1:software-house:zeta#1"),
            _chat("chan:SEAL-1:software-house:alfa#2"),
        ])
        self.assertEqual([r["label"] for r in res["spawns"]],
                         ["alfa#2", "alfa#10", "zeta#1", "zeta#2"])

    def test_broken_session_does_not_break_the_tree(self) -> None:
        bad = SimpleNamespace(
            chat_id="chan:SEAL-1:software-house:rotto#1",
            to_dict=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        res = self._call([bad, _chat("chan:SEAL-1:software-house:ok#1")])
        self.assertEqual([r["label"] for r in res["spawns"]], ["ok#1"])


if __name__ == "__main__":
    unittest.main()
