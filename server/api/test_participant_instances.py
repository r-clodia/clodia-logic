"""Un seed multi-spawn è un super-nodo: una riga per istanza, con il suo stato.

agents-notebook A13:

    «quando un seed è multi spawn dovrebbe vedersi nella lista participant che il
    seed è un super nodo dei vari spawn ognuno con la sua riga»

E la ragione per cui il requisito è arrivato: dopo una seconda menzione di
`fullstack-dev` non si capiva più quante istanze stessero girando, perché
`participants` è una lista di NOMI DI SEED e quattro istanze si leggono come una.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from . import channels


class _Task:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


def _chat(chat_id: str, working: bool):
    return SimpleNamespace(chat_id=chat_id, _current_turn_task=_Task(not working))


class LiveInstancesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = channels.manager.list

    def tearDown(self) -> None:
        channels.manager.list = self._orig

    def _con(self, *chats):
        channels.manager.list = lambda: list(chats)

    def test_each_ordinal_is_its_own_row(self) -> None:
        self._con(_chat("chan:SEAL-1:sh:fullstack-dev#1", True),
                  _chat("chan:SEAL-1:sh:fullstack-dev#2", False))
        vive = channels._live_instances("SEAL-1", "sh", ["fullstack-dev"])
        self.assertEqual([{"ordinal": 1, "state": "working"},
                          {"ordinal": 2, "state": "idle"}],
                         vive["fullstack-dev"])

    def test_rows_are_ordered_by_ordinal(self) -> None:
        self._con(_chat("chan:SEAL-1:sh:fullstack-dev#3", False),
                  _chat("chan:SEAL-1:sh:fullstack-dev#1", False))
        self.assertEqual([1, 3], [r["ordinal"] for r in
                                  channels._live_instances("SEAL-1", "sh",
                                                           ["fullstack-dev"])["fullstack-dev"]])

    def test_a_single_instance_seed_has_no_ordinal(self) -> None:
        """`None`, non `1`: un `#1` suggerirebbe l'esistenza di un `#2`."""
        self._con(_chat("chan:SEAL-1:sh:clodia", True))
        self.assertEqual([{"ordinal": None, "state": "working"}],
                         channels._live_instances("SEAL-1", "sh", ["clodia"])["clodia"])

    def test_a_participant_with_nothing_running_does_not_appear(self) -> None:
        """L'assenza è informazione: è partecipante, e ora non gira niente."""
        self._con(_chat("chan:SEAL-1:sh:clodia", False))
        vive = channels._live_instances("SEAL-1", "sh", ["clodia", "segretario"])
        self.assertIn("clodia", vive)
        self.assertNotIn("segretario", vive)

    def test_sessions_of_another_channel_are_not_counted(self) -> None:
        self._con(_chat("chan:SEAL-1:ALTRO:fullstack-dev#1", True))
        self.assertEqual({}, channels._live_instances("SEAL-1", "sh", ["fullstack-dev"]))

    def test_a_non_participant_is_ignored(self) -> None:
        self._con(_chat("chan:SEAL-1:sh:intruso#1", True))
        self.assertEqual({}, channels._live_instances("SEAL-1", "sh", ["clodia"]))


class ActiveRespondersFindsMultiSpawnTests(unittest.TestCase):
    """Il difetto che faceva sparire la bolla di attività.

    `_active_responders` cercava `chan:<tier>:<name>:<seed>` ESATTO, mentre la
    sessione di un seed multi-spawn si chiama `…:<seed>#<n>`: il `manager.get`
    sollevava `KeyError` e un agente multi-spawn non risultava **mai** attivo.
    Riaprendo il topic a metà turno la UI non mostrava l'indicatore, e l'agente
    sembrava morto mentre lavorava.
    """

    def setUp(self) -> None:
        self._orig = channels.manager.list

    def tearDown(self) -> None:
        channels.manager.list = self._orig

    def test_a_working_multi_spawn_instance_is_reported_active(self) -> None:
        channels.manager.list = lambda: [_chat("chan:SEAL-1:sh:fullstack-dev#2", True)]
        self.assertEqual(["fullstack-dev"],
                         channels._active_responders("SEAL-1", "sh", ["fullstack-dev"]))

    def test_idle_instances_are_not_active(self) -> None:
        channels.manager.list = lambda: [_chat("chan:SEAL-1:sh:fullstack-dev#1", False),
                                         _chat("chan:SEAL-1:sh:fullstack-dev#2", False)]
        self.assertEqual([], channels._active_responders("SEAL-1", "sh", ["fullstack-dev"]))

    def test_one_working_instance_is_enough(self) -> None:
        channels.manager.list = lambda: [_chat("chan:SEAL-1:sh:fullstack-dev#1", False),
                                         _chat("chan:SEAL-1:sh:fullstack-dev#2", True)]
        self.assertEqual(["fullstack-dev"],
                         channels._active_responders("SEAL-1", "sh", ["fullstack-dev"]))

    def test_the_single_instance_case_still_works(self) -> None:
        """Il comportamento di prima non deve regredire: era giusto per i seed
        a istanza singola, ed è la maggioranza."""
        channels.manager.list = lambda: [_chat("chan:SEAL-1:sh:clodia", True)]
        self.assertEqual(["clodia"], channels._active_responders("SEAL-1", "sh", ["clodia"]))


if __name__ == "__main__":
    unittest.main()
